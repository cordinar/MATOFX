"""Process-startup wiring for the observability layer.

Call :func:`configure` exactly once near the top of every entry point —
the FastAPI app, each worker process, the admin CLI, test fixtures.
After configuration, :func:`matofx.obs.get_logger` returns loggers whose
records flow through the JSON-Lines formatter to the configured stream.

``configure`` takes a :class:`matofx.config.LogSettings` directly. There
is no second layer of fallback defaults at this layer — every value the
customer can tune lives in :mod:`matofx.config`, full stop.

In Phase 0 this only sets up structured logging. Prometheus metric
registration and (off-by-default) OpenTelemetry instrumentation are
added later without changing this entry point's signature.
"""

from __future__ import annotations

import logging
from typing import IO, TYPE_CHECKING, Final

from matofx.obs.formatter import JsonLinesFormatter
from matofx.obs.logger import _CONTEXT_FILTER

if TYPE_CHECKING:
    from matofx.config import LogSettings

_HANDLER_NAME: Final[str] = "matofx.obs"


def configure(settings: LogSettings, *, stream: IO[str] | None = None) -> None:
    """Install the JSON-Lines handler on the root logger.

    Args:
        settings: The validated logging configuration. Constructed by
            :func:`matofx.config.load_settings` from the operator's
            ``matofx.toml`` plus ``MATOFX__*`` env-var overrides.
        stream: Where lines are written. ``None`` (the default) resolves
            to :data:`sys.stderr`, which is the I/O convention for app
            logs — container runtimes and systemd capture stderr into
            the platform's log pipeline. This is **not** a customer
            knob; it's a deliberate I/O contract. Tests pass an explicit
            stream to capture output.

    Idempotent: calling it more than once replaces the previous MATOFX
    handler rather than stacking duplicates. Other handlers attached to
    the root logger (e.g. by tests using ``caplog``) are left alone.
    """
    import sys  # noqa: PLC0415 — lazy import keeps the module import-time clean

    target = stream if stream is not None else sys.stderr

    handler = logging.StreamHandler(target)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(
        JsonLinesFormatter(
            ensure_ascii=settings.ensure_ascii,
            redact_fields=settings.redact_fields,
            max_line_bytes=settings.max_line_bytes,
            include_hostname=settings.include_hostname,
        )
    )
    handler.addFilter(_CONTEXT_FILTER)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.level)
