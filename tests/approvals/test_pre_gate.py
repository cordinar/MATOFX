"""Phase 2: the pre gate (queue_or_hold_for_approval + decide_approval).

Service-level, end-to-end against real staging infra (an active
StorageLocation + on-disk bundle): hold, route-to-queue, deny, and
the approve path (which stages + reaches ``queued`` with the
project-EDIT recheck bypassed even though the approver has zero
access to the requestor's project) are all asserted on their real
outcomes.
"""

from __future__ import annotations

import pytest

from matofx.approvals import (
    ApprovalNotPermittedError,
    ApprovalService,
    DenialReasonRequiredError,
)
from matofx.db import session_scope
from matofx.hierarchy.orm import Job

from .conftest import Phase2Env


async def _hold(
    phase2: Phase2Env, approval_service: ApprovalService, policy: str
) -> tuple[int, int]:
    """Submit a draft of ``policy`` and return (job_id, request_id)."""
    job_id, _alias = await phase2.new_draft(policy)
    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.queue_or_hold_for_approval(
            session,
            job_id=job_id,
            actor_user_id=phase2.alice_id,
            inputs={"foo": "held-snapshot"},
        )
        assert job.state == "pending_approval"
    async with session_scope(phase2.factory) as session:
        pending = await approval_service.list_pending_for_user(
            session, username="x", include_all=True
        )
        assert len(pending) == 1
        return job_id, pending[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["pre", "both"])
async def test_policy_holds_job_db_only(
    phase2: Phase2Env, approval_service: ApprovalService, policy: str
) -> None:
    job_id, _alias = await phase2.new_draft(policy)
    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.queue_or_hold_for_approval(
            session,
            job_id=job_id,
            actor_user_id=phase2.alice_id,
            inputs={"foo": "held-snapshot"},
        )
        assert job.state == "pending_approval"
        # DB-only hold: nothing staged.
        assert job.storage_location_id is None
        assert job.working_dir_relative is None
        assert job.inputs == {"foo": "held-snapshot"}

    async with session_scope(phase2.factory) as session:
        pending = await approval_service.list_pending_for_user(session, username="bob")
        assert len(pending) == 1
        req = pending[0]
        assert req.kind == "pre"
        assert req.status == "pending"
        assert req.eligible_approvers == ["bob"]
        assert req.requested_by_user_id == phase2.alice_id


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["none", "post"])
async def test_policy_routes_to_queue(phase2: Phase2Env, policy: str) -> None:
    """none / post don't hold — they delegate to queue_for_runner and
    the job stages + reaches ``queued`` (the hold path never stages,
    so a held job would be ``pending_approval`` with no storage)."""
    job_id, _alias = await phase2.new_draft(policy)
    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.queue_or_hold_for_approval(
            session,
            job_id=job_id,
            actor_user_id=phase2.alice_id,
            inputs={"foo": "bar"},
        )
        assert job.state == "queued"
        assert job.storage_location_id is not None
        assert job.working_dir_relative is not None


@pytest.mark.asyncio
async def test_deny_marks_job_denied(phase2: Phase2Env, approval_service: ApprovalService) -> None:
    job_id, request_id = await _hold(phase2, approval_service, "pre")
    async with session_scope(phase2.factory) as session:
        request, job = await phase2.job_service.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=phase2.bob_id,
            decider_username="bob",
            decision="deny",
            reason="inputs look off",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "denied"
        assert request.decision_reason == "inputs look off"
        assert job.state == "denied"

    async with session_scope(phase2.factory) as session:
        reloaded = await session.get(Job, job_id)
        assert reloaded is not None
        assert reloaded.state == "denied"


@pytest.mark.asyncio
async def test_deny_requires_reason_and_leaves_job_held(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, request_id = await _hold(phase2, approval_service, "pre")

    async def _deny_no_reason() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.decide_approval(
                session,
                request_id=request_id,
                decider_user_id=phase2.bob_id,
                decider_username="bob",
                decision="deny",
                reason="  ",
                decider_is_approver=True,
                decider_is_admin=False,
            )

    with pytest.raises(DenialReasonRequiredError):
        await _deny_no_reason()

    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "pending_approval"


@pytest.mark.asyncio
async def test_approve_bypasses_project_edit_and_queues(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    """Bob is a current approver on the tool but has **no** access to
    Alice's project. Approving must skip the project-EDIT recheck,
    resolve the job under the requestor's identity, and queue it. If
    the recheck weren't bypassed this would fail with
    NotFound/AccessDenied before staging."""
    job_id, request_id = await _hold(phase2, approval_service, "pre")
    async with session_scope(phase2.factory) as session:
        request, job = await phase2.job_service.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=phase2.bob_id,
            decider_username="bob",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "approved"
        assert request.decided_via == "approver"  # eligible, not the requestor
        assert job.state == "queued"
        assert job.storage_location_id is not None

    async with session_scope(phase2.factory) as session:
        reloaded = await session.get(Job, job_id)
        assert reloaded is not None
        assert reloaded.state == "queued"


@pytest.mark.asyncio
async def test_decide_not_permitted_leaves_job_held(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, request_id = await _hold(phase2, approval_service, "pre")

    async def _approve_unauthorised() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.decide_approval(
                session,
                request_id=request_id,
                decider_user_id=phase2.bob_id,
                decider_username="bob",
                decision="approve",
                decider_is_approver=False,  # flag turned off
                decider_is_admin=False,
            )

    with pytest.raises(ApprovalNotPermittedError):
        await _approve_unauthorised()

    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "pending_approval"
