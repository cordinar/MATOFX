"""Approval-request service: create / decide / list-pending / count.

Phase 1 scope: this layer owns the :class:`ApprovalRequest` lifecycle
and its audit trail *only*. The downstream side-effects of a decision
— queueing a pre-approved job, releasing or withholding post results,
the requestor/approver emails — are wired in Phase 2/3 by the job and
runner services that call :meth:`ApprovalService.decide`.

Authorisation is split the same way the jobs router splits
skip-queue: the *caller* (API layer) resolves the live facts it can
see — is this user a current approver? an admin? — and passes them
in; the service trusts those gates and concerns itself with the
request state machine + the ``approver`` / ``self`` / ``admin_override``
classification. Keeping the ``User`` table out of here avoids a
``approvals → users`` import edge and keeps the service unit-testable
with plain booleans.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import select

from matofx.approvals.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalNotPermittedError,
    ApprovalRequestNotFoundError,
    DenialReasonRequiredError,
    DuplicatePendingApprovalError,
    InvalidApprovalKindError,
    SelfApprovalNotAllowedError,
)
from matofx.approvals.orm import APPROVAL_KINDS, ApprovalRequest
from matofx.audit.schema import RecordInput

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter

Decision = Literal["approve", "deny"]

_ACTION_CREATED = "approval.request.created"
_ACTION_APPROVED = "approval.request.approved"
_ACTION_DENIED = "approval.request.denied"
_ACTION_WITHDRAWN = "approval.request.withdrawn"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _canon_usernames(usernames: list[str]) -> list[str]:
    """Lowercase + strip, drop empties, dedupe preserving first-seen order.

    Matches the canonicalisation :attr:`matofx.users.orm.User.username`
    uses, so the snapshot compares cleanly against a live caller's
    username at decision time.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in usernames:
        u = raw.strip().lower()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


@dataclass(frozen=True)
class ApprovalService:
    """Lifecycle + audit for :class:`ApprovalRequest` rows.

    Stateless — construct one per request or share a single instance;
    both are safe.
    """

    audit: AuditWriter

    # ----- create -------------------------------------------------------

    async def create_request(
        self,
        session: AsyncSession,
        *,
        job_id: int | None = None,
        parametric_study_id: int | None = None,
        library_file_version_id: int | None = None,
        material_version_id: int | None = None,
        tool_vv_record_id: int | None = None,
        job_vv_record_id: int | None = None,
        surrogate_model_version_id: int | None = None,
        optimisation_study_id: int | None = None,
        kind: str,
        requested_by_user_id: int,
        eligible_approvers: list[str],
        tool_alias: str | None = None,
        tool_version_id: int | None = None,
        designated_approver_user_id: int | None = None,
        justification: str = "",
    ) -> ApprovalRequest:
        """Open a ``pending`` approval gate for one of the seven target kinds.

        Exactly one of ``job_id`` / ``parametric_study_id`` /
        ``library_file_version_id`` / ``material_version_id`` /
        ``tool_vv_record_id`` / ``job_vv_record_id`` /
        ``surrogate_model_version_id`` must be given.
        Called by the job service for a ``pre``/``both`` job, by the
        runner finalisation for a ``post`` job, by the study service for
        the single study-level gate (design §7), by the library service
        for a global-publish gate (ADR-0006 §5), by the materials
        service for a material-change gate (ADR-0007 §5), and by the
        V&V service for a V&V-record promotion gate (ADR-0011 §3).

        For a **directed** gate (library / materials / V&V), pass
        ``designated_approver_user_id`` — the one named approver the
        submitter chose — with ``eligible_approvers=[their_username]``.
        Self-approval is rejected: the chooser must not be the
        designated approver. ``tool_alias`` / ``tool_version_id`` are
        omitted for a library / material / V&V gate (those gates' "tool"
        context is the V&V record / file / material itself, not a
        simulation tool).

        Raises:
            InvalidApprovalKindError: ``kind`` isn't ``pre``/``post``.
            ValueError: not exactly one target was supplied.
            SelfApprovalNotAllowedError: the submitter chose themselves.
            DuplicatePendingApprovalError: a pending row for this
                ``(target, kind)`` already exists (also enforced by
                the partial unique index — this is the friendly
                pre-check).
        """
        if kind not in APPROVAL_KINDS:
            raise InvalidApprovalKindError(kind)
        targets = [
            job_id,
            parametric_study_id,
            library_file_version_id,
            material_version_id,
            tool_vv_record_id,
            job_vv_record_id,
            surrogate_model_version_id,
            optimisation_study_id,
        ]
        if sum(t is not None for t in targets) != 1:
            raise ValueError(
                "create_request needs exactly one of job_id / parametric_study_id / "
                "library_file_version_id / material_version_id / "
                "tool_vv_record_id / job_vv_record_id / surrogate_model_version_id / "
                "optimisation_study_id"
            )
        if (
            designated_approver_user_id is not None
            and designated_approver_user_id == requested_by_user_id
        ):
            raise SelfApprovalNotAllowedError

        # Pick the single non-None target's column + id for the
        # duplicate-pending pre-check. Order mirrors the targets list.
        target_col, target_id = (
            (ApprovalRequest.parametric_study_id, parametric_study_id)
            if parametric_study_id is not None
            else (ApprovalRequest.library_file_version_id, library_file_version_id)
            if library_file_version_id is not None
            else (ApprovalRequest.material_version_id, material_version_id)
            if material_version_id is not None
            else (ApprovalRequest.tool_vv_record_id, tool_vv_record_id)
            if tool_vv_record_id is not None
            else (ApprovalRequest.job_vv_record_id, job_vv_record_id)
            if job_vv_record_id is not None
            else (ApprovalRequest.surrogate_model_version_id, surrogate_model_version_id)
            if surrogate_model_version_id is not None
            else (ApprovalRequest.optimisation_study_id, optimisation_study_id)
            if optimisation_study_id is not None
            else (ApprovalRequest.job_id, job_id)
        )
        existing = (
            await session.execute(
                select(ApprovalRequest.id)
                .where(target_col == target_id)
                .where(ApprovalRequest.kind == kind)
                .where(ApprovalRequest.status == "pending")
            )
        ).first()
        if existing is not None:
            raise DuplicatePendingApprovalError(int(target_id or 0), kind)

        row = ApprovalRequest(
            job_id=job_id,
            parametric_study_id=parametric_study_id,
            library_file_version_id=library_file_version_id,
            material_version_id=material_version_id,
            tool_vv_record_id=tool_vv_record_id,
            job_vv_record_id=job_vv_record_id,
            surrogate_model_version_id=surrogate_model_version_id,
            optimisation_study_id=optimisation_study_id,
            kind=kind,
            status="pending",
            requested_at=_utcnow(),
            requested_by_user_id=requested_by_user_id,
            eligible_approvers=_canon_usernames(eligible_approvers),
            designated_approver_user_id=designated_approver_user_id,
            justification=(justification or "").strip(),
            tool_alias=tool_alias,
            tool_version_id=tool_version_id,
        )
        session.add(row)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{requested_by_user_id}",
                actor_type="user",
                action=_ACTION_CREATED,
                resource_type="approval_request",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "job_id": job_id,
                    "parametric_study_id": parametric_study_id,
                    "library_file_version_id": library_file_version_id,
                    "material_version_id": material_version_id,
                    "tool_vv_record_id": tool_vv_record_id,
                    "job_vv_record_id": job_vv_record_id,
                    "surrogate_model_version_id": surrogate_model_version_id,
                    "optimisation_study_id": optimisation_study_id,
                    "kind": kind,
                    "tool_alias": tool_alias,
                    "designated_approver_user_id": designated_approver_user_id,
                    "eligible_approvers": list(row.eligible_approvers),
                },
            ),
        )
        return row

    # ----- read ---------------------------------------------------------

    async def get(self, session: AsyncSession, *, request_id: int) -> ApprovalRequest:
        """Fetch one request or raise :class:`ApprovalRequestNotFoundError`."""
        row = await session.get(ApprovalRequest, request_id)
        if row is None:
            raise ApprovalRequestNotFoundError(request_id)
        return row

    async def list_pending_for_user(
        self,
        session: AsyncSession,
        *,
        username: str,
        include_all: bool = False,
    ) -> list[ApprovalRequest]:
        """Pending requests this user should see, oldest first.

        ``include_all=True`` (admin oversight view) returns every
        pending request. Otherwise the snapshot's ``eligible_approvers``
        list is the filter — membership is matched on the canonical
        lowercase username, so a later change to the tool's live
        approver list doesn't change who an already-open request is
        addressed to.

        Filtering is done in Python rather than via a JSON-containment
        SQL predicate: the pending set is small, and a Python filter
        stays identical across SQLite and Postgres (mirrors
        :func:`matofx.tools.approvers.list_tools_referencing_approver`).
        """
        rows = list(
            (
                await session.execute(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.status == "pending")
                    .order_by(
                        ApprovalRequest.requested_at.asc(),
                        ApprovalRequest.id.asc(),
                    )
                )
            ).scalars()
        )
        if include_all:
            return rows
        target = username.strip().lower()
        return [r for r in rows if target in (r.eligible_approvers or [])]

    async def count_pending_for_user(
        self,
        session: AsyncSession,
        *,
        username: str,
        include_all: bool = False,
    ) -> int:
        """Count of :meth:`list_pending_for_user` — powers the home card + nav badge."""
        return len(
            await self.list_pending_for_user(session, username=username, include_all=include_all)
        )

    # ----- decide -------------------------------------------------------

    async def decide(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Decision,
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> ApprovalRequest:
        """Approve or deny a pending request; stamp who/when/how.

        ``decider_is_approver`` / ``decider_is_admin`` are resolved by
        the caller from live state (the user's current ``is_approver``
        flag and admin permission) and trusted here. Authorisation:
        the decider must either be in the request's
        ``eligible_approvers`` snapshot **and** still a current
        approver, **or** be an admin. The ``decided_via``
        classification:

        - ``self`` — the decider is the original requestor *and* an
          eligible current approver (explicit self-approval).
        - ``approver`` — an eligible current approver who isn't the
          requestor.
        - ``admin_override`` — an admin acting outside the eligible
          list (audited distinctly so oversight is visible).

        A denial **requires** a non-empty reason (it's emailed to the
        requestor). The reason is optional on an approval.

        Raises:
            ApprovalRequestNotFoundError, ApprovalAlreadyDecidedError,
            ApprovalNotPermittedError, DenialReasonRequiredError.
        """
        if decision not in ("approve", "deny"):
            msg = f"decision must be 'approve' or 'deny', got {decision!r}"
            raise ValueError(msg)

        row = await self.get(session, request_id=request_id)
        if row.status != "pending":
            raise ApprovalAlreadyDecidedError(request_id, row.status)

        decider = decider_username.strip().lower()
        is_eligible = decider in (row.eligible_approvers or [])
        is_self = decider_user_id == row.requested_by_user_id
        can_decide_as_approver = is_eligible and decider_is_approver

        if not (can_decide_as_approver or decider_is_admin):
            raise ApprovalNotPermittedError

        if can_decide_as_approver and is_self:
            via = "self"
        elif can_decide_as_approver:
            via = "approver"
        else:
            via = "admin_override"

        reason_clean = (reason or "").strip()
        if decision == "deny" and not reason_clean:
            raise DenialReasonRequiredError(request_id)

        row.status = "approved" if decision == "approve" else "denied"
        row.decided_by_user_id = decider_user_id
        row.decided_at = _utcnow()
        # Keep an approve-note if one was given; always keep the
        # mandatory deny reason. Empty approve note → NULL, not "".
        row.decision_reason = reason_clean or None
        row.decided_via = via
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{decider_user_id}",
                actor_type="user",
                action=_ACTION_APPROVED if decision == "approve" else _ACTION_DENIED,
                resource_type="approval_request",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "job_id": row.job_id,
                    "kind": row.kind,
                    "tool_alias": row.tool_alias,
                    "decided_via": via,
                },
                reason=reason_clean if decision == "deny" else None,
            ),
        )
        return row

    async def withdraw(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        actor_user_id: int,
        actor_is_requestor: bool,
        reason: str,
    ) -> ApprovalRequest:
        """Close a pending request without an approver's decision.

        A *withdrawal* is distinct from a denial: the item's owner or a
        governing curator is pulling the request out of review (e.g. the
        underlying V&V record is being retired), not an eligible approver
        judging it. Because the request's terminal states are only
        ``approved`` / ``denied`` (widening the vocabulary would be a
        schema migration), the closed row is stored as ``denied`` — but
        it is audited under a distinct ``approval.request.withdrawn``
        action and ``decided_via`` reflects who withdrew it (``self`` for
        the original requestor, ``admin_override`` for a governing third
        party). No requestor-facing denial email is implied: callers do
        the withdrawal inside the owning service, not via the decide
        router path.

        A reason is mandatory — it lands in the audit trail so a later
        reader sees *why* the gate was closed.

        Raises:
            ApprovalRequestNotFoundError, ApprovalAlreadyDecidedError.
        """
        reason_clean = (reason or "").strip()
        if not reason_clean:
            raise DenialReasonRequiredError(request_id)
        row = await self.get(session, request_id=request_id)
        if row.status != "pending":
            raise ApprovalAlreadyDecidedError(request_id, row.status)
        via = "self" if actor_is_requestor else "admin_override"
        row.status = "denied"
        row.decided_by_user_id = actor_user_id
        row.decided_at = _utcnow()
        row.decision_reason = reason_clean
        row.decided_via = via
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=_ACTION_WITHDRAWN,
                resource_type="approval_request",
                resource_id=str(row.id),
                outcome="success",
                payload={
                    "job_id": row.job_id,
                    "kind": row.kind,
                    "tool_alias": row.tool_alias,
                    "decided_via": via,
                },
                reason=reason_clean,
            ),
        )
        return row


def make_approval_service(audit: AuditWriter) -> ApprovalService:
    """Factory matching the rest of the service-layer style."""
    return ApprovalService(audit=audit)
