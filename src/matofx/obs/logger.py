"""Logger factory.

``get_logger`` returns a stdlib :class:`logging.Logger` with a context
filter attached so every record it emits picks up fields bound via
:func:`matofx.obs.context.bind`.

The filter is attached to the root logger as well during
:func:`matofx.obs.config.configure`, so logs from third-party libraries
also get bound context — but ``get_logger`` is the canonical entry point
inside MATOFX so the contract stays explicit at every call site.
"""

from __future__ import annotations

import logging

from matofx.obs.context import current_context


class _ContextFilter(logging.Filter):
    """Inject context fields onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Mutate the record in place; always allow it through."""
        for key, value in current_context().items():
            # Don't overwrite explicit ``extra`` fields the caller passed.
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


_CONTEXT_FILTER = _ContextFilter()


def get_logger(name: str) -> logging.Logger:
    """Return the MATOFX logger for ``name``, with the context filter attached.

    Idempotent: calling it multiple times with the same name reuses the
    underlying stdlib logger and does not stack the filter.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, _ContextFilter) for f in logger.filters):
        logger.addFilter(_CONTEXT_FILTER)
    return logger
