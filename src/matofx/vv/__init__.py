"""V&V (Verification & Validation) tracking (ADR-0011).

A governed ledger of who verified what, against what evidence, and when
— the auditable record regulated industries (defence, aerospace, pharma,
automotive, medical devices) require to ship simulation results into
production. Two granularities:

- **Tool versions** — "this version of ``heat_transfer`` was validated
  against the X-rig data within ±2% by user Y on date Z." Captured by
  :class:`ToolVVRecord`; inherits across every tool version sharing the
  same bundle hash (design §6.5 / ADR-0011).
- **Job runs** — "this specific run was validated against the matching
  experimental dataset." Captured by :class:`JobVVRecord`; does not
  inherit.

Built in slices (design ``designs/vv-tracking.md`` §11):

- **Slice 0:** ``tool_version.bundle_hash`` + the OS-invariant
  :func:`matofx.tools.versioning.compute_bundle_hash`. The V&V cohort
  key — two tool versions sharing the hash share V&V. Migration
  ``0053``.
- **Slice 1 (this module set):** the data model —
  :class:`ToolVVRecord`, :class:`JobVVRecord`, closed-vocabulary
  ``TOOL_VV_KINDS`` / ``JOB_VV_KINDS`` / ``VV_STATES``, the
  state-transition helpers :func:`is_terminal` /
  :func:`can_transition`. Migration ``0054`` ships the two tables.
- **Slice 2:** ``approval_request`` widening (migration ``0055``) —
  V&V records become a sixth target kind for the existing approval
  mechanism. Two new nullable FK columns (one per record kind), the
  "exactly one target" CHECK widens 4 → 6, and the pending-only
  partial unique index pattern extends.
- **Slice 3:** ``vv_curate`` permission + ``vv_curator`` capability
  role seed + ``vv_required_for_submit`` project-metadata field
  (migration ``0056``).
- **Slice 4+:** :class:`VVService` (CRUD, state machine, bundle-hash
  inheritance query in ``summarise_tool_status``, audit); API;
  submit-form gates + envelope soft-warn; admin surfaces; cross-ADR
  integration (lineage / KPI / Compare badges).

Evidence storage rides on the file library (ADR-0006): a V&V record
references a :class:`LibraryFileVersion` row rather than carrying bytes
itself, so evidence is immutable + content-hashed for free.

The directed-approver gate reuses :mod:`matofx.approvals` —
:class:`VVService` produces an ``approval_request`` row on submit and
the existing decide endpoint promotes the V&V record. The capability
role + General Setting toggle pattern mirrors ``library_curator`` /
``materials_curator`` exactly (ADR-0005).
"""

from __future__ import annotations

from matofx.vv.bootstrap import (
    VV_REQUIRED_FIELD_KEY,
    VV_REQUIRED_VALUES,
    ensure_vv_required_for_submit_field,
)
from matofx.vv.errors import (
    JobVVRecordNotFoundError,
    ToolVVRecordNotFoundError,
    VVApproverRequiredError,
    VVConcurrentModificationError,
    VVDecisionReasonRequiredError,
    VVError,
    VVEvidenceLockError,
    VVMigrationError,
    VVPolicyError,
    VVSoleCuratorError,
    VVStateTransitionError,
)
from matofx.vv.orm import (
    JOB_VV_KINDS,
    TOOL_VV_KINDS,
    VV_STATES,
    JobVVKind,
    JobVVRecord,
    ToolVVKind,
    ToolVVRecord,
    VVState,
    can_transition,
    is_terminal,
)
from matofx.vv.service import (
    EnvelopeWarning,
    PendingVVRecord,
    VVRecordRow,
    VVService,
    VVStatusBadge,
    VVStatusKind,
    make_vv_service,
)

__all__ = [
    "JOB_VV_KINDS",
    "TOOL_VV_KINDS",
    "VV_REQUIRED_FIELD_KEY",
    "VV_REQUIRED_VALUES",
    "VV_STATES",
    "EnvelopeWarning",
    "JobVVKind",
    "JobVVRecord",
    "JobVVRecordNotFoundError",
    "PendingVVRecord",
    "ToolVVKind",
    "ToolVVRecord",
    "ToolVVRecordNotFoundError",
    "VVApproverRequiredError",
    "VVConcurrentModificationError",
    "VVDecisionReasonRequiredError",
    "VVError",
    "VVEvidenceLockError",
    "VVMigrationError",
    "VVPolicyError",
    "VVRecordRow",
    "VVService",
    "VVSoleCuratorError",
    "VVState",
    "VVStateTransitionError",
    "VVStatusBadge",
    "VVStatusKind",
    "can_transition",
    "ensure_vv_required_for_submit_field",
    "is_terminal",
    "make_vv_service",
]
