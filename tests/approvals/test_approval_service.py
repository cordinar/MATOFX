"""ApprovalService: create / get / decide / list-pending / count."""

from __future__ import annotations

import pytest

from matofx.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalNotPermittedError,
    ApprovalRequestNotFoundError,
    ApprovalService,
    DenialReasonRequiredError,
    DuplicatePendingApprovalError,
    InvalidApprovalKindError,
)
from matofx.db import session_scope

from .conftest import SeededJob


async def _open(
    seeded_job: SeededJob,
    approval_service: ApprovalService,
    *,
    kind: str = "pre",
    eligible: list[str] | None = None,
    justification: str = "",
) -> int:
    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.create_request(
            session,
            job_id=seeded_job.job_id,
            kind=kind,
            requested_by_user_id=seeded_job.alice_id,
            eligible_approvers=eligible if eligible is not None else ["bob"],
            tool_alias=seeded_job.tool_alias,
            tool_version_id=seeded_job.tool_version_id,
            justification=justification,
        )
        return row.id


@pytest.mark.asyncio
async def test_create_request_canonicalises_and_strips(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.create_request(
            session,
            job_id=seeded_job.job_id,
            kind="pre",
            requested_by_user_id=seeded_job.alice_id,
            eligible_approvers=["Bob", "  bob ", "ALICE", ""],
            tool_alias=seeded_job.tool_alias,
            tool_version_id=seeded_job.tool_version_id,
            justification="  please run  ",
        )
        assert row.status == "pending"
        assert row.kind == "pre"
        assert row.eligible_approvers == ["bob", "alice"]
        assert row.justification == "please run"
        assert row.decided_via is None


@pytest.mark.asyncio
async def test_create_request_invalid_kind(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(InvalidApprovalKindError):
            await approval_service.create_request(
                session,
                job_id=seeded_job.job_id,
                kind="midway",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )


@pytest.mark.asyncio
async def test_create_request_duplicate_pending(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    await _open(seeded_job, approval_service, kind="pre")
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(DuplicatePendingApprovalError):
            await approval_service.create_request(
                session,
                job_id=seeded_job.job_id,
                kind="pre",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )


@pytest.mark.asyncio
async def test_get_not_found(seeded_job: SeededJob, approval_service: ApprovalService) -> None:
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(ApprovalRequestNotFoundError):
            await approval_service.get(session, request_id=999_999)


@pytest.mark.asyncio
async def test_decide_approve_by_eligible_approver(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    rid = await _open(seeded_job, approval_service, eligible=["bob"])
    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.decide(
            session,
            request_id=rid,
            decider_user_id=seeded_job.bob_id,
            decider_username="bob",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert row.status == "approved"
        assert row.decided_via == "approver"
        assert row.decided_by_user_id == seeded_job.bob_id
        assert row.decided_at is not None
        assert row.decision_reason is None


@pytest.mark.asyncio
async def test_decide_deny_requires_reason(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    rid = await _open(seeded_job, approval_service, eligible=["bob"])
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(DenialReasonRequiredError):
            await approval_service.decide(
                session,
                request_id=rid,
                decider_user_id=seeded_job.bob_id,
                decider_username="bob",
                decision="deny",
                reason="   ",
                decider_is_approver=True,
                decider_is_admin=False,
            )

    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.decide(
            session,
            request_id=rid,
            decider_user_id=seeded_job.bob_id,
            decider_username="bob",
            decision="deny",
            reason="  inputs look wrong  ",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert row.status == "denied"
        assert row.decision_reason == "inputs look wrong"
        assert row.decided_via == "approver"


@pytest.mark.asyncio
async def test_decide_self_when_requestor_is_eligible(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    # Alice is both the requestor and an eligible approver.
    rid = await _open(seeded_job, approval_service, eligible=["alice"])
    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.decide(
            session,
            request_id=rid,
            decider_user_id=seeded_job.alice_id,
            decider_username="alice",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert row.status == "approved"
        assert row.decided_via == "self"


@pytest.mark.asyncio
async def test_decide_admin_override(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    # admin isn't in the eligible list and isn't a current approver,
    # but is an admin → allowed, classified as an override.
    rid = await _open(seeded_job, approval_service, eligible=["bob"])
    async with session_scope(seeded_job.factory) as session:
        row = await approval_service.decide(
            session,
            request_id=rid,
            decider_user_id=seeded_job.admin_id,
            decider_username="admin",
            decision="approve",
            decider_is_approver=False,
            decider_is_admin=True,
        )
        assert row.status == "approved"
        assert row.decided_via == "admin_override"


@pytest.mark.asyncio
async def test_decide_not_permitted_when_listed_but_not_current_approver(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    # Bob is in the snapshot but his is_approver flag was turned off
    # and he isn't an admin → refused.
    rid = await _open(seeded_job, approval_service, eligible=["bob"])
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(ApprovalNotPermittedError):
            await approval_service.decide(
                session,
                request_id=rid,
                decider_user_id=seeded_job.bob_id,
                decider_username="bob",
                decision="approve",
                decider_is_approver=False,
                decider_is_admin=False,
            )


@pytest.mark.asyncio
async def test_decide_already_decided(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    rid = await _open(seeded_job, approval_service, eligible=["bob"])
    async with session_scope(seeded_job.factory) as session:
        await approval_service.decide(
            session,
            request_id=rid,
            decider_user_id=seeded_job.bob_id,
            decider_username="bob",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
    async with session_scope(seeded_job.factory) as session:
        with pytest.raises(ApprovalAlreadyDecidedError):
            await approval_service.decide(
                session,
                request_id=rid,
                decider_user_id=seeded_job.bob_id,
                decider_username="bob",
                decision="deny",
                reason="changed my mind",
                decider_is_approver=True,
                decider_is_admin=False,
            )


@pytest.mark.asyncio
async def test_list_and_count_pending_filter_by_username(
    seeded_job: SeededJob, approval_service: ApprovalService
) -> None:
    # Two pending requests on the same job, distinct kinds (so the
    # (job_id, kind) pending-uniqueness index doesn't collide), with
    # different eligible lists.
    await _open(seeded_job, approval_service, kind="pre", eligible=["bob"])
    await _open(seeded_job, approval_service, kind="post", eligible=["carol"])

    async with session_scope(seeded_job.factory) as session:
        bob_list = await approval_service.list_pending_for_user(session, username="Bob")
        carol_list = await approval_service.list_pending_for_user(session, username="carol")
        everything = await approval_service.list_pending_for_user(
            session, username="nobody", include_all=True
        )
        bob_count = await approval_service.count_pending_for_user(session, username="bob")

    assert [r.kind for r in bob_list] == ["pre"]
    assert [r.kind for r in carol_list] == ["post"]
    assert len(everything) == 2
    assert bob_count == 1
