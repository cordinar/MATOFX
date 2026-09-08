"""Optimisation results-release (``post``) gate — end-to-end (§16.35 / ADR-0009).

Drives a real ``run_study`` outer loop (the in-memory ``FakeRunner`` stands
in for execution, as in ``test_optimisation_runtime``) on a tool whose
descriptor carries a ``post`` / ``both`` policy, then asserts the campaign
is **held** in ``pending_results_approval`` behind one campaign-level gate
rather than completing — and that releasing / denying that single gate
settles the study. Closes the gap that optimisation used to silently drop a
tool's post policy.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.approvals.orm import ApprovalRequest
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.studies.optimisation import make_optimisation_service
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.optimisation.runtime import OptimisationRuntime
from tests.studies.conftest import Basics, seed_gated_tool
from tests.studies.test_optimisation_runtime import FakeRunner


def _running_study(b: Basics, *, tool_alias: str, tool_version_id: int) -> OptimisationStudy:
    """A ``running`` single-objective study targeting the given tool version."""
    config: dict[str, Any] = {
        "target": {"kind": "tool", "tool_alias": tool_alias, "tool_version_id": tool_version_id},
        "factors": [{"name": "x", "type": "float", "min": 0.0, "max": 10.0}],
        "responses": [{"name": "mass", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "random",
        "run_mode": "real",
        "batch_size": 3,
        "concurrency_cap": 3,
        "seed": {"kind": "lhs", "n": 3},
        "stopping_criteria": [{"kind": "max_runs", "value": 6}],
        "random_seed": 42,
    }
    return OptimisationStudy(
        project_id=b.project_id,
        name="gated-opt",
        target_kind="tool",
        tool_alias=tool_alias,
        tool_version_id=tool_version_id,
        config_json=config,
        state="running",
        created_by_user_id=b.user_id,
    )


async def _seed_and_run(
    studies_engine: AsyncEngine, basics: Basics, *, policy: str
) -> tuple[Any, int]:
    """Seed a ``policy`` tool, run a study on it to its stop, return (factory, study_id)."""
    factory = make_session_factory(studies_engine)
    tv_id = await seed_gated_tool(
        studies_engine, basics, alias="gatedopt", policy=policy, approver="bob"
    )
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _running_study(basics, tool_alias="gatedopt", tool_version_id=tv_id)
        session.add(study)
        await session.flush()
        study_id = int(study.id)
    runtime = OptimisationRuntime(
        factory, AuditWriter(signing_key=None, key_id=None), runner=FakeRunner()
    )
    await runtime.run_study(study_id)
    return factory, study_id


async def _post_request_id(factory: Any, study_id: int) -> int:
    async with session_scope(factory) as session:
        req = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.optimisation_study_id == study_id,
                    ApprovalRequest.kind == "post",
                )
            )
        ).scalar_one()
        assert req.status == "pending"
        assert req.eligible_approvers == ["bob"]
        return int(req.id)


@pytest.mark.asyncio
async def test_post_policy_holds_campaign_then_release_completes(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory, study_id = await _seed_and_run(studies_engine, basics, policy="both")

    # Held behind one campaign-level results-release gate (not per-eval).
    async with session_scope(factory) as session:
        study = await session.get(OptimisationStudy, study_id)
        assert study is not None
        assert study.state == "pending_results_approval"
        assert study.stop_reason  # the stop reason that would have completed it
    req_id = await _post_request_id(factory, study_id)

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        _req, study = await service.decide_approval(
            session,
            request_id=req_id,
            decision="approve",
            reason=None,
            decider_user_id=basics.user_id,
            decider_username="bob",
            decider_is_approver=True,
            decider_is_admin=True,
        )
        assert study.state == "complete"

    async with session_scope(factory) as session:
        study = await session.get(OptimisationStudy, study_id)
        assert study is not None
        assert study.state == "complete"
        req = await session.get(ApprovalRequest, req_id)
        assert req is not None
        assert req.status == "approved"


@pytest.mark.asyncio
async def test_post_policy_deny_cancels_and_withholds(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory, study_id = await _seed_and_run(studies_engine, basics, policy="post")
    req_id = await _post_request_id(factory, study_id)

    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        _req, study = await service.decide_approval(
            session,
            request_id=req_id,
            decision="deny",
            reason="results look off",
            decider_user_id=basics.user_id,
            decider_username="bob",
            decider_is_approver=True,
            decider_is_admin=True,
        )
        assert study.state == "cancelled"
        assert study.stop_reason == "results_rejected"

    async with session_scope(factory) as session:
        req = await session.get(ApprovalRequest, req_id)
        assert req is not None
        assert req.status == "denied"


@pytest.mark.asyncio
async def test_no_post_gate_for_none_policy(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    """A tool with no post policy completes straight away — no results gate."""
    factory, study_id = await _seed_and_run(studies_engine, basics, policy="none")
    async with session_scope(factory) as session:
        study = await session.get(OptimisationStudy, study_id)
        assert study is not None
        assert study.state == "complete"
        rows = list(
            (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.optimisation_study_id == study_id
                    )
                )
            ).scalars()
        )
        assert rows == []
