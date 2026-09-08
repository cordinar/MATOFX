"""Migration 0031 schema guarantees: widened job states + the index.

The full ``alembic upgrade head / downgrade base`` round-trip is
already exercised generically by ``tests/db/test_alembic.py`` (0031
is in the chain). These tests pin the *behaviour* the migration is
for: the four new job states are accepted, a bogus one is still
rejected, and the partial unique index allows exactly one pending
request per ``(job_id, kind)``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from matofx.approvals.orm import ApprovalRequest
from matofx.db import session_scope
from matofx.hierarchy.orm import Job

from .conftest import SeededJob

NEW_STATES = ("pending_approval", "denied", "pending_results", "results_withheld")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", NEW_STATES)
async def test_new_job_states_accepted(seeded_job: SeededJob, state: str) -> None:
    factory = seeded_job.factory
    async with session_scope(factory) as session:
        job = await session.get(Job, seeded_job.job_id)
        assert job is not None
        job.state = state
        await session.flush()

    async with session_scope(factory) as session:
        job = await session.get(Job, seeded_job.job_id)
        assert job is not None
        assert job.state == state


@pytest.mark.asyncio
async def test_bogus_job_state_still_rejected(seeded_job: SeededJob) -> None:
    factory = seeded_job.factory

    async def _set_bogus() -> None:
        async with session_scope(factory) as session:
            job = await session.get(Job, seeded_job.job_id)
            assert job is not None
            job.state = "totally_not_a_state"
            await session.flush()

    with pytest.raises(IntegrityError):
        await _set_bogus()


@pytest.mark.asyncio
async def test_one_pending_per_job_kind_enforced_by_index(
    seeded_job: SeededJob,
) -> None:
    """Second *pending* row for the same (job, kind), inserted raw
    (bypassing the service's friendly pre-check), trips the partial
    unique index."""
    factory = seeded_job.factory
    async with session_scope(factory) as session:
        session.add(
            ApprovalRequest(
                job_id=seeded_job.job_id,
                kind="pre",
                status="pending",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )
        )

    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                ApprovalRequest(
                    job_id=seeded_job.job_id,
                    kind="pre",
                    status="pending",
                    requested_by_user_id=seeded_job.alice_id,
                    eligible_approvers=["bob"],
                    tool_alias=seeded_job.tool_alias,
                    tool_version_id=seeded_job.tool_version_id,
                )
            )


@pytest.mark.asyncio
async def test_pending_pre_and_post_coexist(seeded_job: SeededJob) -> None:
    """The index is scoped to (job_id, kind) — a pending pre and a
    pending post for the same job are both allowed."""
    factory = seeded_job.factory
    async with session_scope(factory) as session:
        session.add(
            ApprovalRequest(
                job_id=seeded_job.job_id,
                kind="pre",
                status="pending",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )
        )
        session.add(
            ApprovalRequest(
                job_id=seeded_job.job_id,
                kind="post",
                status="pending",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )
        )
        await session.flush()


@pytest.mark.asyncio
async def test_new_pending_allowed_after_first_decided(
    seeded_job: SeededJob,
) -> None:
    """A decided (non-pending) row doesn't block a fresh pending row
    for the same (job, kind) — uniqueness is scoped to pending."""
    factory = seeded_job.factory
    import datetime as _dt  # noqa: PLC0415

    async with session_scope(factory) as session:
        session.add(
            ApprovalRequest(
                job_id=seeded_job.job_id,
                kind="pre",
                status="denied",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                decided_by_user_id=seeded_job.bob_id,
                decided_at=_dt.datetime.now(tz=_dt.UTC),
                decision_reason="not this time",
                decided_via="approver",
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )
        )

    async with session_scope(factory) as session:
        session.add(
            ApprovalRequest(
                job_id=seeded_job.job_id,
                kind="pre",
                status="pending",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
                tool_alias=seeded_job.tool_alias,
                tool_version_id=seeded_job.tool_version_id,
            )
        )
        await session.flush()
