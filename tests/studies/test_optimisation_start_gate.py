"""Service-level tests for the optimisation start-approval gate (ADR-0009 §14.1).

A tool flagged ``optimisation_start_approval_required`` parks a study in
``pending_start_approval`` at configure (with a directed approval request
to the nominated optimisation_approver); approve → running, reject →
cancelled. An unflagged tool launches straight to running.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

# The directed start-gate request flows through ApprovalRequest, whose FKs
# point at these tables; import their ORM so the shared mapper can resolve
# them in this studies-scoped test (the full-app fixture does this for free).
from matofx.approvals import orm as _approvals_orm  # noqa: F401
from matofx.approvals.orm import ApprovalRequest
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.library import orm as _library_orm  # noqa: F401
from matofx.materials import orm as _materials_orm  # noqa: F401
from matofx.studies.optimisation import (
    OptimisationConfigError,
    make_optimisation_service,
)
from matofx.studies.optimisation.service import _estimate_cost
from matofx.tools.orm import Tool, ToolVersion
from matofx.users import make_password_hasher, make_user_service
from matofx.vv import orm as _vv_orm  # noqa: F401
from tests.studies.conftest import Basics


def test_estimate_cost_is_budget_plus_seed() -> None:
    config = {
        "stopping_criteria": [{"kind": "max_runs", "value": 50}],
        "seed": {"kind": "lhs", "n": 8},
        "batch_size": 4,
    }
    est = _estimate_cost(config)
    assert est == {"max_runs": 50, "seed_runs": 8, "max_total_children": 58, "batch_size": 4}


def test_estimate_cost_cohort_seed_costs_no_new_children() -> None:
    config = {
        "stopping_criteria": [{"kind": "max_runs", "value": 30}],
        "seed": {"kind": "cohort", "cohort_id": 1},
    }
    est = _estimate_cost(config)
    assert est["seed_runs"] == 0
    assert est["max_total_children"] == 30


_GATED_DESCRIPTOR: dict[str, Any] = {
    "alias": "gated",
    "name": "Gated",
    "version": "1.0.0",
    "description": "needs start approval",
    "main_script": "static/main.py",
    "inputs": [{"name": "x", "label": "X", "type": "float", "minimum": 0.0, "maximum": 1.0}],
    "outputs": [{"name": "y", "label": "Y", "type": "number"}],
}


async def _install_gated_tool(factory: Any, *, user_id: int, required: bool) -> int:
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="gated",
                name="Gated",
                version="1.0.0",
                state="released",
                descriptor=_GATED_DESCRIPTOR,
                bundle_path="bundle/gated",
                installed_by_user_id=user_id,
                optimisation_start_approval_required=required,
            )
        )
        tv = ToolVersion(
            alias="gated",
            version="1.0.0",
            descriptor_snapshot=_GATED_DESCRIPTOR,
            bundle_path="bundle/gated",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=user_id,
            change_summary="seed",
            changes={},
        )
        session.add(tv)
        await session.flush()
        return int(tv.id)


async def _make_approver(factory: Any, auth: AuthSettings) -> tuple[int, str]:
    """A second super_user — holds OPTIMISATION_APPROVE via the catalog sentinel."""
    service = make_user_service(
        auth, AuditWriter(signing_key=None, key_id=None), hasher=make_password_hasher(auth)
    )
    async with session_scope(factory) as session:
        user = await service.create_user(
            session,
            username="appr",
            email="appr@example.com",
            password="approverpass1",
            full_name="Approver",
            role="super_user",
        )
        return int(user.id), str(user.username)


async def _bo_config_patch() -> dict[str, Any]:
    return {
        "factors": [{"name": "x", "type": "float", "min": 0.0, "max": 1.0}],
        "responses": [{"name": "y", "spec": {"kind": "scalar"}, "goal": "minimise"}],
        "strategy": "bo",
        "run_mode": "real",
        "batch_size": 2,
        "seed": {"kind": "lhs", "n": 4},
        "stopping_criteria": [{"kind": "max_runs", "value": 20}],
    }


async def _draft_gated_study(factory: Any, basics: Basics, *, required: bool) -> int:
    await _install_gated_tool(factory, user_id=basics.user_id, required=required)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        tv = (
            await session.execute(select(ToolVersion.id).where(ToolVersion.alias == "gated"))
        ).scalar_one()
        study = await service.create_draft(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="gated-opt",
            tool_alias="gated",
            tool_version_id=int(tv),
        )
        sid = study.id
        await service.update_draft(
            session,
            actor_user_id=basics.user_id,
            study_id=sid,
            config_patch=await _bo_config_patch(),
        )
        return sid


async def test_configure_flagged_tool_parks_pending_then_approve_runs(
    studies_engine: AsyncEngine,
    basics: Basics,
    _auth_settings: AuthSettings,  # noqa: PT019 — the AuthSettings value is used to build the approver
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    sid = await _draft_gated_study(factory, basics, required=True)
    approver_id, approver_name = await _make_approver(factory, _auth_settings)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        study = await service.configure(
            session,
            actor_user_id=basics.user_id,
            study_id=sid,
            start_approver_user_id=approver_id,
        )
        assert study.state == "pending_start_approval"
        assert study.start_approver_user_id == approver_id

    # A pending directed request now exists for the study.
    async with session_scope(factory) as session:
        req = (
            await session.execute(
                select(ApprovalRequest).where(ApprovalRequest.optimisation_study_id == sid)
            )
        ).scalar_one()
        assert req.status == "pending"
        assert req.kind == "pre"
        assert req.designated_approver_user_id == approver_id

    async with session_scope(factory) as session:
        approved = await service.approve_start(
            session, actor_user_id=approver_id, actor_username=approver_name, study_id=sid
        )
        assert approved.state == "running"


async def test_configure_flagged_tool_then_reject_cancels(
    studies_engine: AsyncEngine,
    basics: Basics,
    _auth_settings: AuthSettings,  # noqa: PT019 — the AuthSettings value is used to build the approver
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    sid = await _draft_gated_study(factory, basics, required=True)
    approver_id, approver_name = await _make_approver(factory, _auth_settings)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        await service.configure(
            session, actor_user_id=basics.user_id, study_id=sid, start_approver_user_id=approver_id
        )
    async with session_scope(factory) as session:
        rejected = await service.reject_start(
            session,
            actor_user_id=approver_id,
            actor_username=approver_name,
            study_id=sid,
            reason="too expensive",
        )
        assert rejected.state == "cancelled"
        assert rejected.stop_reason == "start_rejected"


async def test_configure_flagged_tool_without_approver_rejected(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    sid = await _draft_gated_study(factory, basics, required=True)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        with pytest.raises(OptimisationConfigError, match="nominated approver"):
            await service.configure(session, actor_user_id=basics.user_id, study_id=sid)


async def test_configure_unflagged_tool_runs_immediately(
    studies_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
    sid = await _draft_gated_study(factory, basics, required=False)
    service = make_optimisation_service(AuditWriter(signing_key=None, key_id=None))
    async with session_scope(factory) as session:
        study = await service.configure(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"
