"""Hash-chained, Ed25519-signed audit log.

Public surface:

- :class:`AuditWriter`, :func:`make_audit_writer` — append API.
- :func:`ensure_chain_head` — create the singleton head row on install.
- :class:`RecordInput` — the caller-supplied record fields.
- :class:`AuditRecordView` — read shape for verifier / UI.
- :func:`verify`, :class:`VerifyResult`, :class:`VerifyFailure` —
  integrity checking.
- :class:`AuditConfigError`, :class:`AuditKeyError` — surfaced errors,
  with plain-English messages.
- :func:`generate_signing_key`, :func:`load_signing_key` —
  key-management helpers, used by the CLI.
"""

from __future__ import annotations

from matofx.audit.keys import (
    AuditKeyError,
    RotationResult,
    collect_public_keys,
    compute_key_id,
    generate_signing_key,
    load_signing_key,
    retired_keys_dir,
    rotate_signing_key,
)
from matofx.audit.schema import (
    AuditActorType,
    AuditOutcome,
    AuditRecordView,
    RecordInput,
)
from matofx.audit.verify import VerifyFailure, VerifyResult, verify
from matofx.audit.writer import (
    AuditConfigError,
    AuditWriter,
    ChainHeadMissingError,
    ensure_chain_head,
    make_audit_writer,
)

__all__ = [
    "AuditActorType",
    "AuditConfigError",
    "AuditKeyError",
    "AuditOutcome",
    "AuditRecordView",
    "AuditWriter",
    "ChainHeadMissingError",
    "RecordInput",
    "RotationResult",
    "VerifyFailure",
    "VerifyResult",
    "collect_public_keys",
    "compute_key_id",
    "ensure_chain_head",
    "generate_signing_key",
    "load_signing_key",
    "make_audit_writer",
    "retired_keys_dir",
    "rotate_signing_key",
    "verify",
]
