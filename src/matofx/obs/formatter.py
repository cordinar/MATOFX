"""JSON-Lines :class:`logging.Formatter` for MATOFX.

Each log record becomes a single JSON object on a single line. The schema
is intentionally small and stable so log shippers (Loki, Splunk, plain
``grep``) can rely on it.

Mandatory fields on every record:

- ``timestamp`` — ISO-8601 UTC with microseconds, suffixed ``Z``.
- ``level`` — log level name, lower-case (``"info"``, ``"warning"``, ...).
- ``logger`` — the ``name`` of the originating logger.
- ``message`` — the rendered message string.
- ``process_id``, ``process_name`` — useful for multi-instance deployments.
- ``hostname`` — host the record was emitted from (when enabled in settings).

Optional fields, included when present:

- ``request_id``, ``user_id``, plus anything else bound via
  :func:`matofx.obs.context.bind` — flattened into the top-level object.
- ``exception`` — set to a string traceback when ``logger.exception(...)``
  or ``exc_info=True`` is used.
- ``extra`` keys passed via ``logger.info("...", extra={...})`` —
  flattened, except when they would collide with a reserved field, in
  which case they're nested under ``extra``.

Reserved field names cannot be overwritten by user code; collisions are
re-routed under ``extra`` rather than silently dropped.

## Security

Two protections are baked in to defend against credential leakage and
volume-DoS:

- **Redaction**: any field name whose lower-case form contains a
  built-in or customer-supplied substring (``password``, ``token``,
  ``secret``, ``cookie``, ``authorization``, ``credential``,
  ``api_key``, ``private_key``, plus :class:`LogSettings.redact_fields`)
  has its value replaced with ``"***REDACTED***"``. Recurses into
  nested dicts and lists.
- **Per-line size cap**: lines exceeding
  :class:`LogSettings.max_line_bytes` are replaced with a small
  truncation marker carrying the timestamp / level / logger / original
  size. Defends against a single runaway record dumping megabytes
  into the operator's log pipeline.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import socket
from collections.abc import Iterable
from typing import Any, Final

# Standard ``logging.LogRecord`` attribute names. Anything not in this set
# that appears on the record is treated as an "extra" field passed by the
# caller (or attached by a context filter).
_LOGRECORD_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)

# Field names the formatter owns and won't let user-supplied ``extra`` overwrite.
_FORMATTER_RESERVED: Final[frozenset[str]] = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "message",
        "exception",
        "process_id",
        "process_name",
        "hostname",
        "truncated_bytes",
    }
)

# Built-in field-name substrings whose values are always redacted. The
# customer-supplied ``LogSettings.redact_fields`` extends this list at
# install time. ``session`` is intentionally omitted — ``session_id``
# is meaningful in audit context and isn't a credential.
BUILT_IN_REDACT_PATTERNS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "credential",
        "cookie",
        "authorization",
    }
)

REDACTED_VALUE: Final[str] = "***REDACTED***"

# Captured once at import time. The hostname is fixed for the life of
# the process; re-resolving it per record is a waste.
_HOSTNAME: Final[str] = socket.gethostname()


class JsonLinesFormatter(logging.Formatter):
    r"""Format log records as one JSON object per line.

    Args:
        ensure_ascii: Forwarded to :func:`json.dumps`. When ``False`` (the
            recommended setting), non-ASCII characters render as themselves
            rather than ``\uXXXX`` escapes.
        redact_fields: Customer-supplied additional substrings to redact.
            Combined with :data:`BUILT_IN_REDACT_PATTERNS`. Case-insensitive
            substring match against the field name.
        max_line_bytes: Per-line ceiling. Records that would serialise
            larger than this are replaced with a small truncation marker.
        include_hostname: Whether to emit a ``hostname`` field on every
            record. Hostname is captured once at process start.
    """

    def __init__(
        self,
        *,
        ensure_ascii: bool,
        redact_fields: Iterable[str] = (),
        max_line_bytes: int = 16_384,
        include_hostname: bool = True,
    ) -> None:
        """Construct the formatter; see class docstring for arguments."""
        super().__init__()
        self._ensure_ascii = ensure_ascii
        self._redact_patterns = frozenset(
            {p.lower() for p in BUILT_IN_REDACT_PATTERNS} | {p.lower() for p in redact_fields}
        )
        self._max_line_bytes = max_line_bytes
        self._include_hostname = include_hostname

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as one JSON line. See class docstring for details."""
        payload: dict[str, Any] = {
            "timestamp": self._format_timestamp(record.created),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "process_id": record.process,
            "process_name": record.processName,
        }
        if self._include_hostname:
            payload["hostname"] = _HOSTNAME

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text

        # Anything attached to the record that isn't a stdlib field or our
        # own private bookkeeping is treated as an "extra" field.
        extras: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key in _LOGRECORD_RESERVED or key.startswith("_"):
                continue
            extras[key] = value

        if extras:
            redacted_extras = self._redact(extras)
            collisions: dict[str, Any] = {}
            for key, value in redacted_extras.items():
                if key in _FORMATTER_RESERVED:
                    collisions[key] = value
                else:
                    payload[key] = value
            if collisions:
                payload["extra"] = collisions

        serialised = json.dumps(payload, ensure_ascii=self._ensure_ascii, default=repr)
        if len(serialised.encode("utf-8")) > self._max_line_bytes:
            return self._truncation_marker(payload, original_serialised=serialised)
        return serialised

    @staticmethod
    def _format_timestamp(epoch_seconds: float) -> str:
        return (
            _dt.datetime.fromtimestamp(epoch_seconds, tz=_dt.UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _redact(self, value: Any) -> Any:
        """Recursively replace values whose key matches a redact pattern."""
        if isinstance(value, dict):
            return {
                key: REDACTED_VALUE if self._is_redacted_key(key) else self._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _is_redacted_key(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower()
        return any(pattern in lowered for pattern in self._redact_patterns)

    def _truncation_marker(self, payload: dict[str, Any], *, original_serialised: str) -> str:
        """Build a small stand-in record for an over-cap log line."""
        original_bytes = len(original_serialised.encode("utf-8"))
        marker: dict[str, Any] = {
            "timestamp": payload["timestamp"],
            "level": payload["level"],
            "logger": payload["logger"],
            "message": (
                f"[log line truncated; original was {original_bytes} bytes, "
                f"cap is {self._max_line_bytes}]"
            ),
            "process_id": payload.get("process_id"),
            "process_name": payload.get("process_name"),
            "truncated_bytes": original_bytes,
        }
        if "hostname" in payload:
            marker["hostname"] = payload["hostname"]
        return json.dumps(marker, ensure_ascii=self._ensure_ascii)
