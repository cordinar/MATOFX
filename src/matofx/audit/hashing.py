"""Deterministic record hashing.

The audit chain's integrity rests on every record having a SHA-256 hash
that is reproducible byte-for-byte from the record's content. Two
implementers must agree on the canonical bytes of a record, so this
module owns the canonicalisation rules:

- JSON, sorted keys, no whitespace, ASCII-escape disabled (UTF-8 stays).
- Timestamps serialise as ISO-8601 microsecond UTC with a ``Z`` suffix —
  the same shape we use elsewhere in the codebase.
- ``None`` values are emitted explicitly (not omitted) so the field set
  is fixed and unambiguous.
- The ``hash`` and ``signature`` fields are excluded from the canonical
  bytes (they're derivatives of those bytes, by definition).
- The ``prior_hash`` field IS included — it's what links the chain.

Any change here is a chain-incompatible break; bump
:data:`matofx.audit.schema.CURRENT_SCHEMA_VERSION` if it ever changes,
and have ``verify()`` honour the old recipe for old records.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from typing import Any, Final

# The 32-byte all-zero genesis hash. The first record in the chain has
# ``prior_hash`` equal to this value.
GENESIS_HASH: Final[str] = "0" * 64


_FIELDS_IN_HASH_ORDER: Final[tuple[str, ...]] = (
    "schema_version",
    "timestamp",
    "actor",
    "actor_type",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "request_id",
    "session_id",
    "source_ip",
    "payload",
    "reason",
    "correlation_id",
    "key_id",
    "prior_hash",
)


def canonical_bytes(record: dict[str, Any]) -> bytes:
    """Return the canonical UTF-8 bytes of ``record`` for hashing.

    ``record`` must contain every field listed in
    :data:`_FIELDS_IN_HASH_ORDER`; missing keys raise ``KeyError`` so a
    silent omission can't produce a hash that looks valid.
    """
    canonical: dict[str, Any] = {
        field: _normalise(record[field]) for field in _FIELDS_IN_HASH_ORDER
    }
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_hash(record: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical record bytes."""
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def _normalise(value: Any) -> Any:
    """Convert non-JSON-native scalars into a deterministic JSON form."""
    if isinstance(value, _dt.datetime):
        return _format_timestamp(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _format_timestamp(ts: _dt.datetime) -> str:
    """ISO-8601 UTC with microseconds and a ``Z`` suffix."""
    ts = ts.replace(tzinfo=_dt.UTC) if ts.tzinfo is None else ts.astimezone(_dt.UTC)
    return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")
