"""Streaming status events for the migrate export / import surface.

The export and import paths take an ``on_event`` callback and emit one
:class:`ProgressEvent` per substantive milestone (signature verified,
tool imported, tool skipped with reason, summary line, …). The UI
streams these events into the scrolling status log in the admin
page; the CLI prints them to stdout; the test suite collects them
into a list and asserts on the sequence.

The event type is a tiny frozen dataclass with no I/O — the
producer / consumer split keeps the service layer testable without
mocking a real renderer.

Event ``level`` is a closed vocabulary:

* ``info`` — neutral progress: "Verifying signature…", "Reading bundle
  contents…", "Importing tools…".
* ``imported`` — one item landed successfully. ``item_kind``,
  ``item_alias``, ``item_version`` populated.
* ``skipped`` — one item refused with a plain-English reason in
  ``reason``. ``item_*`` populated.
* ``warning`` — informational, non-fatal (e.g. an approver username
  was dropped because that user doesn't exist on the destination).
* ``error`` — something the bundle as a whole can't recover from
  (signature mismatch, malformed manifest, no trust). The producer
  raises immediately after emitting these.
* ``summary`` — final tally line at the end of the run.

The ``message`` field is the plain-English line the operator sees
exactly as written. The catalog of every string is split across
:mod:`matofx.migrate.errors` (for per-item skip reasons) and the
service modules themselves (for neutral progress lines).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal

ProgressLevel = Literal[
    "info",
    "imported",
    "skipped",
    "warning",
    "error",
    "summary",
]
"""Closed vocabulary for :attr:`ProgressEvent.level`."""

ProgressItemKind = Literal["tool", "workflow", "bundle"]
"""Closed vocabulary for :attr:`ProgressEvent.item_kind`."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One status line emitted during export or import.

    ``message`` is the operator-facing string. The other fields carry
    enough structured detail for the UI to highlight failures, the
    CLI to colourise output, and the test suite to assert on outcomes
    without parsing prose.
    """

    level: ProgressLevel
    message: str
    item_kind: ProgressItemKind | None = None
    item_alias: str | None = None
    item_version: str | None = None
    reason: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


# Type alias for the callback the service methods accept. ``None`` is
# treated as a no-op by every emitter — saves the caller from writing
# ``lambda _: None`` everywhere.
OnEvent = Callable[[ProgressEvent], None] | None


def emit(on_event: OnEvent, event: ProgressEvent) -> None:
    """Invoke ``on_event(event)`` if a callback was supplied.

    Tiny helper so the service modules don't repeat the ``if on_event
    is not None`` guard at every emission point.
    """
    if on_event is not None:
        on_event(event)


# ---------------------------------------------------------------------------
# Convenience constructors — the producers use these instead of building
# a ProgressEvent inline so the level/kind enums stay typo-safe.
# ---------------------------------------------------------------------------


def info(message: str, **extra: str) -> ProgressEvent:
    """Neutral progress line — no item attribution."""
    return ProgressEvent(level="info", message=message, extra=dict(extra))


def imported_tool(*, alias: str, version: str, message: str) -> ProgressEvent:
    """A tool was imported successfully."""
    return ProgressEvent(
        level="imported",
        message=message,
        item_kind="tool",
        item_alias=alias,
        item_version=version,
    )


def imported_workflow(*, alias: str, message: str) -> ProgressEvent:
    """A workflow was imported successfully."""
    return ProgressEvent(
        level="imported",
        message=message,
        item_kind="workflow",
        item_alias=alias,
    )


def skipped_tool(*, alias: str, version: str, reason: str) -> ProgressEvent:
    """A tool was refused with a plain-English reason."""
    return ProgressEvent(
        level="skipped",
        message=reason,
        item_kind="tool",
        item_alias=alias,
        item_version=version,
        reason=reason,
    )


def skipped_workflow(*, alias: str, reason: str) -> ProgressEvent:
    """A workflow was refused with a plain-English reason."""
    return ProgressEvent(
        level="skipped",
        message=reason,
        item_kind="workflow",
        item_alias=alias,
        reason=reason,
    )


def warning(message: str, **extra: str) -> ProgressEvent:
    """Non-fatal informational note (e.g. a dropped approver)."""
    return ProgressEvent(level="warning", message=message, extra=dict(extra))


def error(message: str) -> ProgressEvent:
    """Something the bundle as a whole can't recover from."""
    return ProgressEvent(level="error", message=message)


def summary(message: str, **extra: str) -> ProgressEvent:
    """Final tally line at the end of the run."""
    return ProgressEvent(level="summary", message=message, extra=dict(extra))


__all__: Final = [
    "OnEvent",
    "ProgressEvent",
    "ProgressItemKind",
    "ProgressLevel",
    "emit",
    "error",
    "imported_tool",
    "imported_workflow",
    "info",
    "skipped_tool",
    "skipped_workflow",
    "summary",
    "warning",
]
