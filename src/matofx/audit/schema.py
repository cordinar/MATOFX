"""The 18-field audit-record schema and the input shape callers provide.

Per §D21 of the brief, every audit record carries 18 fields. Five are
computed by the writer (``prior_hash``, ``hash``, ``signature``,
``key_id``, ``schema_version``); the other 13 come from the caller.

This module defines:

- :class:`AuditOutcome`, :class:`AuditActorType` — enums for the closed
  vocabulary fields.
- :class:`RecordInput` — what callers pass to :meth:`AuditWriter.record`.
- :class:`AuditRecordView` — the materialised, fully-computed record
  returned by reads / verification.

Field semantics — the brief is the canonical reference. Brief notes:

- ``actor`` — opaque string identifying the actor (e.g. ``"user:42"``,
  ``"service:gpu-scheduler"``, ``"system"``).
- ``action`` — dotted action verb, e.g. ``"login.success"``,
  ``"job.submit"``, ``"approval.delegate"``.
- ``resource_type`` / ``resource_id`` — what was acted on. Resource id
  is stringified so any ID type (UUID, int, slug) fits.
- ``payload`` — JSON-serialisable dict; structured details that don't fit
  the named fields. Bounded; never use it for huge blobs.
- ``reason`` — optional plain-English why-text (denials, manual changes).
- ``correlation_id`` — caller-supplied identifier that ties multiple
  records into one logical operation across processes.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CURRENT_SCHEMA_VERSION = 1
"""Bump when the field set changes; verify() must keep handling old versions."""


AuditOutcome = Literal["success", "failure", "denied"]
AuditActorType = Literal["user", "service", "system", "anonymous"]


class _Frozen(BaseModel):
    """Frozen base for audit shapes — extra fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class RecordInput(_Frozen):
    """Caller-supplied fields for a new audit record.

    The writer computes ``prior_hash``, ``hash``, ``signature``,
    ``key_id``, and ``schema_version`` itself.
    """

    timestamp: _dt.datetime | None = None
    """When the audited event happened. Defaults to ``now()`` at write time."""

    actor: str
    actor_type: AuditActorType
    action: str
    resource_type: str
    resource_id: str
    outcome: AuditOutcome
    request_id: str | None = None
    session_id: str | None = None
    source_ip: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    correlation_id: str | None = None


class AuditRecordView(_Frozen):
    """A fully-materialised audit record, as persisted.

    This is the read shape — what verify() walks, what the admin
    "Cleanup activity" view renders. Fields are immutable; corrections
    are appended as new records, never mutations of old ones.
    """

    id: int
    timestamp: _dt.datetime
    actor: str
    actor_type: AuditActorType
    action: str
    resource_type: str
    resource_id: str
    outcome: AuditOutcome
    request_id: str | None
    session_id: str | None
    source_ip: str | None
    prior_hash: str
    hash: str
    signature: str | None
    key_id: str | None
    schema_version: int
    payload: dict[str, Any]
    reason: str | None
    correlation_id: str | None
