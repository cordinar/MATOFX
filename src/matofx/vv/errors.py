"""User-facing errors for the V&V service (ADR-0011).

Plain English; safe to render directly to the end user. Shape parallels
:mod:`matofx.library.errors` / :mod:`matofx.materials.errors` so the API
exception handler can register one umbrella handler (:class:`VVError`)
and treat the domain uniformly. The curator-permission gate reuses
:class:`matofx.users.errors.PermissionDeniedError` (already mapped to
403), so there's no V&V-specific access-denied class here.

Slice 1 lands the schema + ORM-shape errors actually raised by the ORM
layer (the "not found" reads + the optimistic-lock collision). The full
service-layer error set (state-transition, sole-curator guard, policy
gate, evidence lock, decision-reason-required) lands with the service
in slice 4; the symbols are pre-declared here so the import surface
doesn't grow between slices.
"""

from __future__ import annotations


class VVError(Exception):
    """Base class for V&V domain errors."""


# ---- Slice 1 (ORM + read side) --------------------------------------


class ToolVVRecordNotFoundError(VVError):
    """No tool V&V record with that id."""

    def __init__(self, record_id: int) -> None:
        """Construct with the missing record id."""
        super().__init__("We couldn't find that V&V record.")
        self.record_id = record_id


class JobVVRecordNotFoundError(VVError):
    """No job V&V record with that id."""

    def __init__(self, record_id: int) -> None:
        """Construct with the missing record id."""
        super().__init__("We couldn't find that V&V record.")
        self.record_id = record_id


class VVConcurrentModificationError(VVError):
    """Another writer changed this V&V record since the caller loaded it.

    Raised when the optimistic-lock ``version`` on the row has moved on
    between load and save. Reload and re-apply the change.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This V&V record was changed by someone else while you were editing. "
            "Please reload and try again."
        )


# ---- Slice 4 (service-layer state machine + gates) ------------------


class VVStateTransitionError(VVError):
    """An attempted V&V record state transition isn't allowed.

    Names the offending ``from → to`` pair. The lifecycle table lives
    in :mod:`matofx.vv.orm` and is read via :func:`can_transition`.
    """

    def __init__(self, from_state: str, to_state: str) -> None:
        """Construct with the offending transition."""
        super().__init__(f"V&V records can't move from {from_state!r} to {to_state!r}.")
        self.from_state = from_state
        self.to_state = to_state


class VVApproverRequiredError(VVError):
    """A V&V record was submitted with the approval toggle ON but no approver chosen.

    With the ``vv_record_approval`` General Setting ON (default), promoting
    a draft to ``pending`` requires the author to name one ``vv_curator``
    (other than themselves) to review it (design §6.1). Self-approval is
    rejected separately by the approvals service
    (:class:`matofx.approvals.errors.SelfApprovalNotAllowedError`).
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This V&V record needs a reviewer. Choose one V&V curator "
            "(other than yourself) to approve it."
        )


class VVSoleCuratorError(VVError):
    """V&V submit attempted but the install has only one ``vv_curator`` holder.

    With the ``vv_record_approval`` General Setting ON and exactly one
    ``vv_curator`` (the would-be author), there is no valid approver
    other than self — submit cannot proceed. The fix is either to grant
    the ``vv_curator`` capability role to a second user, or to turn off
    the approval toggle (design §6.3).
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "Can't submit this V&V record for review — only one V&V curator "
            "exists. Either grant the V&V curator role to another user, or "
            "turn off the `vv_record_approval` setting to allow direct "
            "approval by a single curator."
        )


class VVDecisionReasonRequiredError(VVError):
    """A V&V record reject was attempted without a decision reason.

    ``reject`` always requires a reason so the audit trail and the
    rejected-record badge tooltip have something to display. ``approve``
    accepts but does not require one.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__("Rejecting a V&V record needs a reason.")


class VVPolicyError(VVError):
    """A job submission is blocked by the per-project ``vv_required_for_submit`` policy.

    The project has the metadata field set to ``yes`` and the chosen
    tool version's V&V badge isn't ``verified_validated`` (design §6.4).
    The message names the tool version so the operator can act on the
    rejection.
    """

    def __init__(self, tool_alias: str, tool_version: str) -> None:
        """Construct with the tool whose V&V state blocked submission."""
        super().__init__(
            f"This project requires V&V'd tools only — {tool_alias} v{tool_version} "
            "doesn't have an approved verification + validation record yet."
        )
        self.tool_alias = tool_alias
        self.tool_version = tool_version


class VVEvidenceLockError(VVError):
    """An attempt to retire / replace a library file blocked because V&V records cite it.

    Raised by the library-side retire path when a
    :attr:`ToolVVRecord.evidence_file_version_id` or
    :attr:`JobVVRecord.evidence_file_version_id` still references the
    version. The fix is to retire the V&V records first (or point them
    at a new evidence version), then retire the file. Mirrors the
    materials-side ``MaterialRetiredError`` shape.
    """

    def __init__(self, library_file_version_id: int, citing_count: int) -> None:
        """Construct with the locked file version + number of citing records."""
        super().__init__(
            "This evidence file can't be retired — "
            f"{citing_count} V&V record(s) still reference it. "
            "Retire those V&V records first, or point them at a "
            "replacement evidence file."
        )
        self.library_file_version_id = library_file_version_id
        self.citing_count = citing_count


class VVMigrationError(VVError):
    """A tool-record migration request is invalid (design §13).

    Raised when the source and target tool versions aren't a valid pair
    for carrying V&V forward — e.g. they belong to different tools, are
    the same version, or one of them doesn't exist. Per-record problems
    (a record that isn't approved, or doesn't belong to the source) are
    reported per-item as *skips*, not raised as this whole-request error.
    """

    def __init__(self, message: str) -> None:
        """Construct with a plain-English explanation of the bad request."""
        super().__init__(message)
