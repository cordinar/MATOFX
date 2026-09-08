"""Directed-approver + library-target approval gates (ADR-0006 §5).

The file library (and, later, materials) reuse ``matofx.approvals`` for
a single named-approver gate. These tests pin the third approval target
(``library_file_version_id``), the no-self-approval rule, the exactly-one
target invariant, and that the chosen approver can decide while others
can't.
"""

from __future__ import annotations

import pytest

from matofx.approvals import (
    ApprovalNotPermittedError,
    ApprovalService,
    SelfApprovalNotAllowedError,
)
from matofx.db import session_scope
from matofx.library import LibraryFile, LibraryFileVersion

from .conftest import SeededJob


async def _make_library_version(seeded_job: SeededJob) -> int:
    """Create a personal library file + one version owned by alice; return version id."""
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        lf = LibraryFile(
            name="Baseline mesh",
            description="shared",
            scope="personal",
            owner_user_id=seeded_job.alice_id,
            created_by_user_id=seeded_job.alice_id,
        )
        session.add(lf)
        await session.flush()
        v = LibraryFileVersion(
            library_file_id=lf.id,
            ordinal=1,
            stored_path="/mnt/lib/1/1/mesh.msh",
            original_filename="mesh.msh",
            content_hash="b" * 64,
            size_bytes=2048,
            extension="msh",
            provenance="from gmsh",
            created_by_user_id=seeded_job.alice_id,
        )
        session.add(v)
        await session.flush()
        return int(v.id)


@pytest.mark.asyncio
async def test_create_library_gate_with_directed_approver(
    approval_service: ApprovalService, seeded_job: SeededJob
) -> None:
    version_id = await _make_library_version(seeded_job)
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        req = await approval_service.create_request(
            session,
            library_file_version_id=version_id,
            kind="pre",
            requested_by_user_id=seeded_job.alice_id,
            eligible_approvers=["bob"],
            designated_approver_user_id=seeded_job.bob_id,
        )
        assert req.library_file_version_id == version_id
        assert req.job_id is None
        assert req.parametric_study_id is None
        assert req.designated_approver_user_id == seeded_job.bob_id
        assert req.tool_alias is None  # a library gate has no tool
        assert req.tool_version_id is None
        assert req.eligible_approvers == ["bob"]


@pytest.mark.asyncio
async def test_self_approval_rejected(
    approval_service: ApprovalService, seeded_job: SeededJob
) -> None:
    version_id = await _make_library_version(seeded_job)
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(SelfApprovalNotAllowedError):
            await approval_service.create_request(
                session,
                library_file_version_id=version_id,
                kind="pre",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["alice"],
                designated_approver_user_id=seeded_job.alice_id,  # chooser == self
            )


@pytest.mark.asyncio
async def test_exactly_one_target_enforced(
    approval_service: ApprovalService, seeded_job: SeededJob
) -> None:
    version_id = await _make_library_version(seeded_job)
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        # Two targets at once.
        with pytest.raises(ValueError, match="exactly one"):
            await approval_service.create_request(
                session,
                job_id=seeded_job.job_id,
                library_file_version_id=version_id,
                kind="pre",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
            )
        # No target at all.
        with pytest.raises(ValueError, match="exactly one"):
            await approval_service.create_request(
                session,
                kind="pre",
                requested_by_user_id=seeded_job.alice_id,
                eligible_approvers=["bob"],
            )


@pytest.mark.asyncio
async def test_designated_approver_decides_others_cannot(
    approval_service: ApprovalService, seeded_job: SeededJob
) -> None:
    version_id = await _make_library_version(seeded_job)
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        req = await approval_service.create_request(
            session,
            library_file_version_id=version_id,
            kind="pre",
            requested_by_user_id=seeded_job.alice_id,
            eligible_approvers=["bob"],
            designated_approver_user_id=seeded_job.bob_id,
        )
        request_id = req.id

    # A non-eligible, non-admin user can't decide.
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        with pytest.raises(ApprovalNotPermittedError):
            await approval_service.decide(
                session,
                request_id=request_id,
                decider_user_id=seeded_job.admin_id,
                decider_username="carol",
                decision="approve",
                decider_is_approver=False,
                decider_is_admin=False,
            )

    # The designated approver (a current curator) approves.
    async with session_scope(seeded_job.factory) as session:  # type: ignore[arg-type]
        decided = await approval_service.decide(
            session,
            request_id=request_id,
            decider_user_id=seeded_job.bob_id,
            decider_username="bob",
            decision="approve",
            decider_is_approver=True,
            decider_is_admin=False,
        )
        assert decided.status == "approved"
        assert decided.decided_via == "approver"
