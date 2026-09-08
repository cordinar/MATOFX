"""Phase 3: the post gate — decide_approval(post) + output lockdown.

The runner-side intercept (``_open_post_approval_if_required``) is
covered in ``tests/runner/test_supervisor.py``. Here we pin the
service contract: releasing / withholding already-produced results,
and the central ``ensure_outputs_accessible`` rule that every output
surface funnels through.
"""

from __future__ import annotations

import pytest

from matofx.approvals import ApprovalService, DenialReasonRequiredError
from matofx.db import session_scope
from matofx.hierarchy.errors import NotFoundError, ResultsWithheldError
from matofx.hierarchy.orm import Job

from .conftest import Phase2Env


async def _held_post(
    phase2: Phase2Env,
    approval_service: ApprovalService,
    *,
    eligible: tuple[str, ...] = ("bob",),
) -> tuple[int, int]:
    """Simulate the runner's post gate: a finished run of a post-policy
    tool, held in ``pending_results`` with an open ``post`` request.

    Returns (job_id, request_id).
    """
    job_id, _alias = await phase2.new_draft("post", approvers=eligible)
    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        request = await approval_service.create_request(
            session,
            job_id=job_id,
            kind="post",
            requested_by_user_id=phase2.alice_id,
            eligible_approvers=list(eligible),
            tool_alias=job.tool_alias,
            tool_version_id=job.tool_version_id,
        )
        job.state = "pending_results"
        return job_id, request.id


@pytest.mark.asyncio
async def test_post_approve_releases_results(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, request_id = await _held_post(phase2, approval_service)
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
        assert request.decided_via == "approver"
        assert job.state == "complete"  # released

    async with session_scope(phase2.factory) as session:
        reloaded = await session.get(Job, job_id)
        assert reloaded is not None
        assert reloaded.state == "complete"


@pytest.mark.asyncio
async def test_post_deny_withholds_results(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, request_id = await _held_post(phase2, approval_service)
    async with session_scope(phase2.factory) as session:
        request, job = await phase2.job_service.decide_approval(
            session,
            request_id=request_id,
            decider_user_id=phase2.bob_id,
            decider_username="bob",
            decision="deny",
            reason="export-controlled output",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert request.status == "denied"
        assert request.decision_reason == "export-controlled output"
        assert job.state == "results_withheld"

    async with session_scope(phase2.factory) as session:
        reloaded = await session.get(Job, job_id)
        assert reloaded is not None
        assert reloaded.state == "results_withheld"


@pytest.mark.asyncio
async def test_post_deny_requires_reason_and_keeps_pending(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, request_id = await _held_post(phase2, approval_service)

    async def _deny_no_reason() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.decide_approval(
                session,
                request_id=request_id,
                decider_user_id=phase2.bob_id,
                decider_username="bob",
                decision="deny",
                reason="",
                decider_is_approver=True,
                decider_is_admin=False,
            )

    with pytest.raises(DenialReasonRequiredError):
        await _deny_no_reason()

    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        assert job.state == "pending_results"


# --- output lockdown (ensure_outputs_accessible) --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending_results", "results_withheld"])
async def test_requestor_is_blocked_from_withheld_outputs(
    phase2: Phase2Env, approval_service: ApprovalService, state: str
) -> None:
    """Alice owns the project (so normally has VIEW) but is the
    requestor, not an approver — she's refused while gated."""
    job_id, _request_id = await _held_post(phase2, approval_service)
    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = state

    async def _access() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.ensure_outputs_accessible(
                session,
                job_id=job_id,
                actor_user_id=phase2.alice_id,
                actor_username="alice",
                actor_is_approver=False,
                actor_is_admin=False,
            )

    with pytest.raises(ResultsWithheldError):
        await _access()


@pytest.mark.asyncio
async def test_eligible_approver_sees_withheld_outputs_without_project_access(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    """Bob has no access to Alice's project but is an eligible
    approver — his approver authority overrides the project ACL."""
    job_id, _request_id = await _held_post(phase2, approval_service, eligible=("bob",))
    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.ensure_outputs_accessible(
            session,
            job_id=job_id,
            actor_user_id=phase2.bob_id,
            actor_username="Bob",  # case-insensitive match
            actor_is_approver=True,
            actor_is_admin=False,
        )
        assert job.state == "pending_results"


@pytest.mark.asyncio
async def test_admin_sees_withheld_outputs(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    job_id, _request_id = await _held_post(phase2, approval_service, eligible=("carol",))
    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.ensure_outputs_accessible(
            session,
            job_id=job_id,
            actor_user_id=phase2.admin_id,
            actor_username="admin",
            actor_is_approver=False,
            actor_is_admin=True,
        )
        assert job.state == "pending_results"


@pytest.mark.asyncio
async def test_non_eligible_approver_without_access_is_existence_hidden(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    """Bob is a current approver but not eligible on *this* request,
    and has no project access → 404 (existence hidden), not 403."""
    job_id, _request_id = await _held_post(phase2, approval_service, eligible=("carol",))

    async def _access() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.ensure_outputs_accessible(
                session,
                job_id=job_id,
                actor_user_id=phase2.bob_id,
                actor_username="bob",
                actor_is_approver=True,
                actor_is_admin=False,
            )

    with pytest.raises(NotFoundError):
        await _access()


@pytest.mark.asyncio
async def test_normal_state_uses_plain_view_gate(
    phase2: Phase2Env, approval_service: ApprovalService
) -> None:
    """A non-withheld job: owner gets it; an outsider is 404."""
    job_id, _request_id = await _held_post(phase2, approval_service)
    async with session_scope(phase2.factory) as session:
        job = await session.get(Job, job_id)
        assert job is not None
        job.state = "complete"

    async with session_scope(phase2.factory) as session:
        job = await phase2.job_service.ensure_outputs_accessible(
            session,
            job_id=job_id,
            actor_user_id=phase2.alice_id,
            actor_username="alice",
            actor_is_approver=False,
            actor_is_admin=False,
        )
        assert job.state == "complete"

    async def _outsider() -> None:
        async with session_scope(phase2.factory) as session:
            await phase2.job_service.ensure_outputs_accessible(
                session,
                job_id=job_id,
                actor_user_id=phase2.bob_id,
                actor_username="bob",
                actor_is_approver=False,
                actor_is_admin=False,
            )

    with pytest.raises(NotFoundError):
        await _outsider()
