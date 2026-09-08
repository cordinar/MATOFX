"""Canonical bytes for a compliance report (ADR-0019 §B6).

A report is signed by the customer's install and verified by Funis,
possibly years later and possibly by a reimplementation. Both sides must
agree byte-for-byte on what was signed, so the canonicalisation rules
live here and nowhere else.

Rules — deliberately the same recipe as :mod:`matofx.audit.hashing`, so
there is one canonicalisation idiom in the codebase rather than two:

- JSON, sorted keys, no whitespace, ``ensure_ascii=False`` (UTF-8 stays).
- Timestamps serialise as ISO-8601 microsecond UTC with a ``Z`` suffix.
- ``None`` is emitted explicitly, never omitted — a field that is absent
  and a field that is null must not hash the same.
- The ``signature`` object is excluded (it is a derivative of these
  bytes, by definition). Everything else is included, including fields a
  future schema version adds: the recipe is *whole-document*, so a
  verifier that doesn't understand a new field still verifies the
  signature over it.

The last point is why this module walks the document generically instead
of listing fields like the audit recipe does. A report is a nested,
evolving document; an audit record is a fixed row.

Any change to these rules is a signature-incompatible break: bump
:data:`matofx.compliance.schema.REPORT_SCHEMA_VERSION` and teach the
verifier the old recipe for old reports.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any, Final

SIGNATURE_KEY: Final[str] = "signature"
"""Top-level key excluded from the canonical bytes."""


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Return the canonical UTF-8 bytes of ``document`` for signing.

    The top-level ``signature`` key is dropped if present, so the same
    function serves both "sign this" and "verify what was signed".
    """
    payload = {k: _normalise(v) for k, v in document.items() if k != SIGNATURE_KEY}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def content_sha256(document: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical bytes (used to fingerprint a report)."""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def format_timestamp(ts: _dt.datetime) -> str:
    """ISO-8601 UTC with microseconds and a ``Z`` suffix."""
    ts = ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts.astimezone(_dt.UTC)
    return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalise(value: Any) -> Any:
    """Convert non-JSON-native scalars into a deterministic JSON form."""
    if isinstance(value, _dt.datetime):
        return format_timestamp(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    return value
