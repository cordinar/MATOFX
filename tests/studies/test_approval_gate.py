"""§7 single study-level approval gate (slice 2-PS-a/7).

The §13 non-negotiable: an approval-gated tool ⇒ the *whole study*
holds at one gate (never per child). ``fake_queue`` (conftest) stands
in for the real staging on the approve-path enqueue; decisions use the
admin-override path so these tests pin the *study* side-effects, not
ApprovalService's three-layer defence (covered by tests/approvals).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from matofx.approvals.orm import ApprovalRequest
from matofx.db import make_session_factory, session_scope
from matofx.studies import StudyService, StudyStateError
from tests.studies.conftest import Basics, seed_gated_tool


def _svc() -> StudyService:
    from matofx.audit import AuditWriter  # noqa: PLC0415

    return StudyService(audit=AuditWriter(signing_key=None, key_id=None), environment="test")


_SPEC = {
    "a": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": [40, 60]}},
    "b": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": [60, 40]}},
}


async def _draft(factory, svc: StudyService, basics: Basics, alias: str) -> int:
    async with session_scope(factory) as session:
        study = await svc.create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="gated sweep",
            tool_alias=alias,
            mode="list",
            factor_spec=_SPEC,
        )
        return study.id


async def _requests(factory, sid: int) -> list[ApprovalRequest]:
    async with session_scope(factory) as session:
        return list(
            (
                await session.execute(
                    select(ApprovalRequest).where(ApprovalRequest.parametric_study_id == sid)
                )
            ).scalars()
        )


async def test_pre_gate_holds_then_approve_enqueues(
    studies_engine, basics: Basics, fake_queue: list[int]
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    await seed_gated_tool(studies_engine, basics, alias="gpre", policy="pre", approver="sam")
    sid = await _draft(factory, svc, basics, "gpre")

    async with session_scope(factory) as session:
        study = await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "pending_approval"
        # No children created while held.
        kids = await svc._list_children(session, sid)
        assert kids == []

    reqs = await _requests(factory, sid)
    assert len(reqs) == 1  # ONE gate for the whole study, never per child
    assert reqs[0].job_id is None
    assert reqs[0].kind == "pre"
    assert reqs[0].status == "pending"
    rid = reqs[0].id

    async with session_scope(factory) as session:
        request, study = await svc.decide_approval(
            session,
            request_id=rid,
            decider_user_id=basics.user_id,
            decider_username="sam",
            decision="approve",
            decider_is_approver=False,
            decider_is_admin=True,  # admin override
        )
        assert request.status == "approved"
        assert study.state == "running"
        kids = await svc._list_children(session, sid)
        assert len(kids) == 2
        assert all(k.state == "queued" for k in kids)
    assert sorted(fake_queue) == sorted(k.id for k in kids)


async def test_pre_gate_reject_freezes_study(studies_engine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    await seed_gated_tool(studies_engine, basics, alias="grej", policy="pre", approver="sam")
    sid = await _draft(factory, svc, basics, "grej")
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
    rid = (await _requests(factory, sid))[0].id

    async with session_scope(factory) as session:
        _request, study = await svc.decide_approval(
            session,
            request_id=rid,
            decider_user_id=basics.user_id,
            decider_username="sam",
            decision="deny",
            reason="not this sweep",
            decider_is_approver=False,
            decider_is_admin=True,
        )
        assert study.state == "rejected"
        assert await svc._list_children(session, sid) == []
    # Frozen — a rejected study can't be resubmitted (clone instead).
    async with session_scope(factory) as session:
        with pytest.raises(StudyStateError, match="only a draft"):
            await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        await session.rollback()


async def test_post_gate_holds_at_terminal_then_releases(
    studies_engine,
    basics: Basics,
    fake_queue: list[int],  # noqa: ARG001 — fakes enqueue
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    await seed_gated_tool(studies_engine, basics, alias="gpost", policy="post", approver="sam")
    sid = await _draft(factory, svc, basics, "gpost")

    async with session_scope(factory) as session:
        study = await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"  # post policy → runs immediately
        kids = await svc._list_children(session, sid)
        for k in kids:
            k.state = "complete"
        await session.flush()

    # Runner-style system rollup: children all terminal + post policy
    # → hold the whole study at one post gate, don't conclude.
    async with session_scope(factory) as session:
        study = await svc.refresh_state(session, actor_user_id=None, study_id=sid)
        assert study.state == "pending_approval"
    reqs = await _requests(factory, sid)
    assert len(reqs) == 1
    assert reqs[0].kind == "post"
    rid = reqs[0].id

    async with session_scope(factory) as session:
        _request, study = await svc.decide_approval(
            session,
            request_id=rid,
            decider_user_id=basics.user_id,
            decider_username="sam",
            decision="approve",
            decider_is_approver=False,
            decider_is_admin=True,
        )
        assert study.state == "complete"  # released → real terminal


async def test_none_policy_runs_immediately(
    studies_engine, basics: Basics, fake_queue: list[int]
) -> None:
    """Regression: a non-gated tool still fans + queues at submit."""
    factory = make_session_factory(studies_engine)
    svc = _svc()
    await seed_gated_tool(studies_engine, basics, alias="gnone", policy="none", approver="")
    sid = await _draft(factory, svc, basics, "gnone")
    async with session_scope(factory) as session:
        study = await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"
        kids = await svc._list_children(session, sid)
        assert len(kids) == 2
    assert sorted(fake_queue) == sorted(k.id for k in kids)
    assert await _requests(factory, sid) == []  # no gate
