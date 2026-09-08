"""StudyService lifecycle tests (create+pinning, ACL, submit fan, cancel).

DB-backed (SQLite + Postgres via the slice-1 conftest). The
``basics`` user is a super_user (resolves to OWNER on any project);
``outsider_user_id`` has no grant (NONE -> existence-hiding 404).
"""

from __future__ import annotations

import pytest

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.errors import NotFoundError
from matofx.studies import (
    NoValidPointsError,
    RunCapExceededError,
    StudyService,
    StudyStateError,
)
from matofx.studies.orm import ParametricStudy
from tests.studies.conftest import Basics


def _svc() -> StudyService:
    from matofx.audit import AuditWriter  # noqa: PLC0415

    return StudyService(audit=AuditWriter(signing_key=None, key_id=None))


def _list_spec(a: list[float], b: list[float]) -> dict[str, object]:
    return {
        "a": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": a}},
        "b": {"role": "vary", "type": "numeric", "vary": {"kind": "list", "values": b}},
    }


async def _make_draft(
    factory,
    svc: StudyService,
    basics: Basics,
    alias: str,
    *,
    factor_spec: dict[str, object] | None = None,
    mode: str = "list",
    run_cap: int = 200,
) -> int:
    async with session_scope(factory) as session:
        study = await svc.create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="sweep-1",
            tool_alias=alias,
            mode=mode,
            factor_spec=factor_spec,
            run_cap=run_cap,
        )
        return study.id


# --- create ---------------------------------------------------------------


async def test_create_pins_tool_version(studies_engine, basics: Basics, valid_tool: str) -> None:
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        study = await _svc().create(
            session,
            actor_user_id=basics.user_id,
            project_id=basics.project_id,
            name="  My Study  ",
            tool_alias=valid_tool,
            mode="grid",
        )
        latest = (
            await session.execute(
                select(ToolVersion.id)
                .where(ToolVersion.alias == valid_tool)
                .order_by(ToolVersion.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
    assert study.state == "draft"
    assert study.name == "My Study"
    assert study.tool_version_id == latest
    assert study.run_cap == 200


async def test_create_rejects_bad_mode(studies_engine, basics: Basics, valid_tool: str) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        with pytest.raises(StudyStateError, match="sampling mode"):
            await _svc().create(
                session,
                actor_user_id=basics.user_id,
                project_id=basics.project_id,
                name="x",
                tool_alias=valid_tool,
                mode="d_optimal",
            )
        await session.rollback()


async def test_create_rejects_empty_name(studies_engine, basics: Basics, valid_tool: str) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        with pytest.raises(StudyStateError, match="needs a name"):
            await _svc().create(
                session,
                actor_user_id=basics.user_id,
                project_id=basics.project_id,
                name="   ",
                tool_alias=valid_tool,
                mode="list",
            )
        await session.rollback()


async def test_create_unknown_tool(studies_engine, basics: Basics) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):
            await _svc().create(
                session,
                actor_user_id=basics.user_id,
                project_id=basics.project_id,
                name="x",
                tool_alias="ghost",
                mode="list",
            )
        await session.rollback()


async def test_create_rejects_surrogate_tool(studies_engine, basics: Basics) -> None:
    """A surrogate tool can't be a study's fitting tool (ADR-0008 §7.5).

    The descriptor's ``surrogate_model_version_id`` marks it; sampling a
    surrogate to fit a surrogate is meaningless, so create rejects it with
    a plain-English message.
    """
    import datetime as _dt  # noqa: PLC0415

    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    snapshot = {"alias": "surrogate_sweep_3", "surrogate_model_version_id": 3}
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        session.add(
            Tool(
                alias="surrogate_sweep_3",
                name="Surrogate of sweep",
                version="1.0",
                state="released",
                descriptor=snapshot,
                bundle_path="bundle/surrogate_sweep_3",
                installed_by_user_id=basics.user_id,
            )
        )
        session.add(
            ToolVersion(
                alias="surrogate_sweep_3",
                version="1.0",
                descriptor_snapshot=snapshot,
                bundle_path="bundle/surrogate_sweep_3",
                created_at=_dt.datetime.now(tz=_dt.UTC),
                created_by_user_id=basics.user_id,
                change_summary="seed",
                changes={},
            )
        )
        await session.flush()
        with pytest.raises(StudyStateError, match="surrogate tool"):
            await _svc().create(
                session,
                actor_user_id=basics.user_id,
                project_id=basics.project_id,
                name="meta",
                tool_alias="surrogate_sweep_3",
                mode="list",
            )
        await session.rollback()


async def test_create_denied_for_outsider(
    studies_engine, basics: Basics, valid_tool: str, outsider_user_id: int
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        with pytest.raises(NotFoundError):  # NONE access -> existence-hiding
            await _svc().create(
                session,
                actor_user_id=outsider_user_id,
                project_id=basics.project_id,
                name="x",
                tool_alias=valid_tool,
                mode="list",
            )
        await session.rollback()


# --- get / list ACL -------------------------------------------------------


async def test_get_and_list_for_creator_outsider_denied(
    studies_engine, basics: Basics, valid_tool: str, outsider_user_id: int
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(factory, svc, basics, valid_tool)
    async with session_scope(factory) as session:
        got = await svc.get(session, actor_user_id=basics.user_id, study_id=sid)
        assert got.id == sid
        listed = await svc.list_for_project(
            session, actor_user_id=basics.user_id, project_id=basics.project_id
        )
        assert [s.id for s in listed] == [sid]
        with pytest.raises(NotFoundError):
            await svc.get(session, actor_user_id=outsider_user_id, study_id=sid)


# --- submit ---------------------------------------------------------------


async def test_submit_fans_children_and_runs(
    studies_engine, basics: Basics, valid_tool: str, fake_queue: list[int]
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    # a+b: (40,60)=100 ok, (40,40)=80 skip, (60,60)=120 skip, (60,40)=100 ok -> 2 valid
    sid = await _make_draft(
        factory, svc, basics, valid_tool, factor_spec=_list_spec([40, 60], [60, 40])
    )
    async with session_scope(factory) as session:
        study = await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "running"
        assert study.generated_count == 2
        assert study.skipped_invalid_count == 2
        children = await svc.list_children(session, actor_user_id=basics.user_id, study_id=sid)
    assert len(children) == 2
    assert sorted(fake_queue) == sorted(c.id for c in children)
    for c in children:
        assert c.scenario_id is None
        assert c.parametric_study_id == sid
        assert c.tool_version_id == study.tool_version_id
        assert c.state == "queued"  # slice 4: submit enqueues each child
        assert c.inputs["a"] + c.inputs["b"] == 100.0


async def test_submit_twice_rejected(
    studies_engine,
    basics: Basics,
    valid_tool: str,
    fake_queue: list[int],  # noqa: ARG001 — fakes submit()'s enqueue
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(factory, svc, basics, valid_tool, factor_spec=_list_spec([40], [60]))
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
    async with session_scope(factory) as session:
        with pytest.raises(StudyStateError, match="only a draft"):
            await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        await session.rollback()


async def test_submit_no_valid_points(studies_engine, basics: Basics, valid_tool: str) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(
        factory, svc, basics, valid_tool, factor_spec=_list_spec([10], [10])
    )  # 10+10 != 100 -> every point skipped
    async with session_scope(factory) as session:
        with pytest.raises(NoValidPointsError, match="no runnable points"):
            await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        await session.rollback()


async def test_submit_run_cap_exceeded(studies_engine, basics: Basics, valid_tool: str) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(
        factory,
        svc,
        basics,
        valid_tool,
        factor_spec=_list_spec([40, 60], [60, 40]),
        run_cap=1,
    )
    async with session_scope(factory) as session:
        with pytest.raises(RunCapExceededError, match="limit is 1"):
            await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        await session.rollback()


# --- cancel + refresh -----------------------------------------------------


async def test_cancel_is_idempotent_and_cascades(
    studies_engine,
    basics: Basics,
    valid_tool: str,
    fake_queue: list[int],  # noqa: ARG001 — fakes submit()'s enqueue
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(
        factory, svc, basics, valid_tool, factor_spec=_list_spec([40, 60], [60, 40])
    )
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        children = await svc._list_children(session, sid)
        children[0].state = "running"  # pretend the runner picked one up
        await session.flush()
    async with session_scope(factory) as session:
        study = await svc.cancel(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "cancelled"
        assert study.cancel_requested_at is not None
        kids = await svc._list_children(session, sid)
        # Slice 4: submit enqueues children, so they're queued/running —
        # cancel signals every non-terminal child for the runner loop.
        assert all(c.cancel_requested_at is not None for c in kids)
    async with session_scope(factory) as session:
        again = await svc.cancel(session, actor_user_id=basics.user_id, study_id=sid)
        assert again.state == "cancelled"  # idempotent, no error


async def test_refresh_state_rolls_up(
    studies_engine,
    basics: Basics,
    valid_tool: str,
    fake_queue: list[int],  # noqa: ARG001 — fakes submit()'s enqueue
) -> None:
    factory = make_session_factory(studies_engine)
    svc = _svc()
    sid = await _make_draft(
        factory, svc, basics, valid_tool, factor_spec=_list_spec([40, 60], [60, 40])
    )
    async with session_scope(factory) as session:
        await svc.submit(session, actor_user_id=basics.user_id, study_id=sid)
        kids = await svc._list_children(session, sid)
        kids[0].state = "complete"
        kids[1].state = "failed"
        await session.flush()
    async with session_scope(factory) as session:
        study = await svc.refresh_state(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "partial"
    async with session_scope(factory) as session:
        for c in await svc._list_children(session, sid):
            c.state = "complete"
        await session.flush()
    async with session_scope(factory) as session:
        study = await svc.refresh_state(session, actor_user_id=basics.user_id, study_id=sid)
        assert study.state == "complete"


async def test_studies_orm_importable() -> None:
    """Guards the public surface used by later slices."""
    assert ParametricStudy.__tablename__ == "parametric_study"
