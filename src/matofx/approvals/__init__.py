"""Job approvals — the pre/post gates from §16.15.

A tool's captured descriptor declares an ``approvals_policy``
(``none`` / ``pre`` / ``post`` / ``both``). When a job is submitted
against a ``pre``/``both`` tool it's held in ``pending_approval``
until an approver decides; when a ``post``/``both`` job's run
succeeds its outputs are withheld (``pending_results``) until an
approver releases them. Each gate is one :class:`ApprovalRequest`
row, kept forever (decision history is durable, never deleted).

This package owns the request lifecycle + its hash-chained audit
trail. The job/runner services call into :class:`ApprovalService`
to open requests and apply the post-decision side-effects
(queueing, result release/withholding, notifications).

Public surface:

- :class:`ApprovalRequest` — ORM row backing ``approval_request``.
- :class:`ApprovalService` — create / decide / list-pending / count,
  with the ``approver`` / ``self`` / ``admin_override`` classification.
- :data:`APPROVAL_KINDS` / :data:`APPROVAL_STATUSES` /
  :data:`APPROVAL_DECIDED_VIA` — the closed vocabularies (mirrored by
  CHECK constraints created in migration ``0031``).
- The error hierarchy rooted at :class:`ApprovalError`.
"""

from __future__ import annotations

from matofx.approvals.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalError,
    ApprovalNotPermittedError,
    ApprovalRequestNotFoundError,
    DenialReasonRequiredError,
    DuplicatePendingApprovalError,
    InvalidApprovalKindError,
    SelfApprovalNotAllowedError,
)
from matofx.approvals.orm import (
    APPROVAL_DECIDED_VIA,
    APPROVAL_KINDS,
    APPROVAL_STATUSES,
    ApprovalRequest,
)
from matofx.approvals.service import (
    ApprovalService,
    Decision,
    make_approval_service,
)

__all__ = [
    "APPROVAL_DECIDED_VIA",
    "APPROVAL_KINDS",
    "APPROVAL_STATUSES",
    "ApprovalAlreadyDecidedError",
    "ApprovalError",
    "ApprovalNotPermittedError",
    "ApprovalRequest",
    "ApprovalRequestNotFoundError",
    "ApprovalService",
    "Decision",
    "DenialReasonRequiredError",
    "DuplicatePendingApprovalError",
    "InvalidApprovalKindError",
    "SelfApprovalNotAllowedError",
    "make_approval_service",
]
