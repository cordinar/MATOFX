"""Tests for the job-cohort lifecycle + live membership (ADR-0008 §3).

SQLite + Postgres via the shared ``studies`` conftest (schema built by
``alembic upgrade head``, so these also exercise migrations 0058 + 0059).
Covers create validation, project-ACL get/list, live membership
(state / tool-version / input-range filtering), the cross-project
ACL-intersect hide, and the tolerated-version divergence warnings.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import AuthSettings
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_project_service, make_scenario_service
from matofx.hierarchy.errors import AccessDeniedError, NotFoundError
from matofx.hierarchy.orm import Job, ProjectAcl
from matofx.studies.cohort import CohortConfigError, CohortStateError, make_cohort_service
from matofx.studies.cohort.orm import JobCohort  # noqa: F401  -- registers the mapper
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.tools.exec_orm import StorageLocation
from matofx.tools.orm import Tool, ToolVersion
from matofx.users import make_password_hasher, make_user_service
from matofx.users.orm import User
from matofx.users.permissions import Permission
from matofx.users.role_orm import Role, RolePermission, UserCapabilityRole
from tests.studies.conftest import Basics


async def _grant_cohort_curator(session, user_id: int) -> None:
    """Stack the built-in ``cohort_curator`` capability role on a user.

    The role + its ``cohorts.curate`` grant are seeded by migration 0074
    (run as part of ``upgrade head``); this just adds the membership row.
    """
    role_id = (
        await session.execute(select(Role.id).where(Role.name == "cohort_curator"))
    ).scalar_one()
    session.add(UserCapabilityRole(user_id=user_id, role_id=role_id))
    await session.flush()


async def _revoke_permission(session, user_id: int, permission: Permission) -> None:
    """Drop ``permission`` from the user's base role.

    Used to prove a gate is still a gate after a permission moved to
    base tier — "everyone has it by default" and "nothing checks it"
    look identical from the outside otherwise.
    """
    role_id = (await session.execute(select(User.role_id).where(User.id == user_id))).scalar_one()
    await session.execute(
        delete(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission == permission.value,
        )
    )
    await session.flush()


async def _regular_user(session, auth: AuthSettings, *, username: str) -> int:
    """Create a plain ``regular_user`` (no capability roles) and return its id."""
    user_service = make_user_service(auth, _audit(), hasher=make_password_hasher(auth))
    user = await user_service.create_user(
        session,
        username=username,
        email=f"{username}@example.com",
        password=f"{username}password",
        full_name=username.title(),
        role="regular_user",
    )
    return user.id


def _regular_auth() -> AuthSettings:
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


def _service():
    return make_cohort_service(_audit())


_NAME_SEQ = itertools.count(1)


async def _scenario(session, basics: Basics, *, project_id: int | None = None) -> int:
    """A fresh scenario (unique name) so same-tool jobs don't collide on
    ``uq_job_scenario_tool_standalone`` (one tool per scenario)."""
    scenario = await make_scenario_service(_audit()).create(
        session,
        actor_user_id=basics.user_id,
        project_id=project_id or basics.project_id,
        name=f"S-{next(_NAME_SEQ)}",
    )
    return scenario.id


async def _complete_job(
    session,
    basics: Basics,
    *,
    scenario_id: int | None = None,
    project_id: int | None = None,
    inputs: dict | None = None,
    tool_version_id: int | None = None,
    completed_at: _dt.datetime | None = None,
    state: str = "complete",
    created_by_user_id: int | None = None,
) -> Job:
    if scenario_id is None:
        scenario_id = await _scenario(session, basics, project_id=project_id)
    job = Job(
        scenario_id=scenario_id,
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=tool_version_id or basics.tool_version_id,
        state=state,
        inputs=inputs or {},
        created_by_user_id=created_by_user_id or basics.user_id,
        completed_at=completed_at,
    )
    session.add(job)
    await session.flush()
    return job


async def _commissioned_study(session, basics: Basics, *, n: int) -> tuple[int, list[Job]]:
    """A parametric study plus ``n`` completed children homed to it.

    A commissioned run is homed under the study, not a scenario
    (``ck_job_exactly_one_home``), which is why it cannot be dropped by editing
    ``member_job_ids``.
    """
    study = ParametricStudy(
        project_id=basics.project_id,
        name=f"commissioned-{next(_NAME_SEQ)}",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )
    session.add(study)
    await session.flush()
    jobs = []
    for _ in range(n):
        job = Job(
            parametric_study_id=study.id,
            scenario_id=None,
            name="commissioned-run",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs={},
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        jobs.append(job)
    await session.flush()
    return int(study.id), jobs


# --- create / validation -------------------------------------------------


async def test_create_and_get(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="  Past heat-sink runs  ",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        assert cohort.id is not None
        assert cohort.name == "Past heat-sink runs"  # trimmed
        assert cohort.state == "active"
        assert cohort.auto_grow is True  # live until the snapshot slice (D4)
        fetched = await svc.get(session, actor_user_id=basics.user_id, cohort_id=cohort.id)
        assert fetched.id == cohort.id


async def test_create_empty_tolerated_defaults_to_latest(studies_engine, basics: Basics) -> None:
    """An empty tolerated list anchors on the tool's latest version (§3.2).

    This is the UI's create path — the form never asks for a version id.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[],
        )
        assert cohort.tolerated_tool_version_ids == [basics.tool_version_id]


async def test_create_rejects_version_of_other_tool(studies_engine, basics: Basics) -> None:
    """A tolerated version must belong to the cohort's tool (§3.2)."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(CohortConfigError):
            await svc.create_cohort(
                session,
                actor_user_id=basics.user_id,
                name="c",
                tool_alias="not_the_tool",
                tolerated_tool_version_ids=[basics.tool_version_id],
            )


async def test_create_rejects_bad_input_range(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(CohortConfigError):
            await svc.create_cohort(
                session,
                actor_user_id=basics.user_id,
                name="c",
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
                input_ranges={"fin_height": {"min": 40, "max": 5}},
            )


async def test_archive_hides_from_default_list_and_blocks_edit(
    studies_engine, basics: Basics
) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        await svc.archive_cohort(session, actor_user_id=basics.user_id, cohort_id=cohort.id)
        assert cohort.state == "archived"
        assert cohort.archived_at is not None

        active = await svc.list_all(session, actor_user_id=basics.user_id)
        assert cohort.id not in {c.id for c in active}
        allc = await svc.list_all(session, actor_user_id=basics.user_id, include_archived=True)
        assert cohort.id in {c.id for c in allc}

        with pytest.raises(CohortStateError):
            await svc.update_cohort(
                session, actor_user_id=basics.user_id, cohort_id=cohort.id, name="new"
            )


# --- live membership (§3.1) ---------------------------------------------


async def test_resolve_members_filters_state_tool_and_version(
    studies_engine, basics: Basics
) -> None:
    """Only completed jobs of the tool + a tolerated version are members."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # A second, NOT-tolerated version of the same tool.
        other_v = ToolVersion(
            alias=basics.tool_alias,
            version="2.0",
            descriptor_snapshot={"alias": basics.tool_alias},
            bundle_path="bundle/demo2",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="v2",
            changes={},
        )
        session.add(other_v)
        await session.flush()

        member = await _complete_job(session, basics)
        await _complete_job(session, basics, state="running")  # running → excluded
        await _complete_job(  # not-tolerated version → excluded
            session, basics, tool_version_id=other_v.id
        )

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [member.id]
        assert resolved.acl_hidden == 0
        assert cohort.last_resolved_at is not None

        # A read-only resolve (the cohort list, a fit's ingestion) must not write:
        # two concurrent lists stamping the same rows made one fail.
        stamped = cohort.last_resolved_at
        read_only = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id, stamp=False
        )
        assert read_only.job_ids == [member.id]
        assert cohort.last_resolved_at == stamped
        assert cohort not in session.dirty


async def test_resolve_members_input_range_filter(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        in_range = await _complete_job(session, basics, inputs={"h": 20})
        await _complete_job(session, basics, inputs={"h": 99})  # out of range → not a member
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            input_ranges={"h": {"min": 5, "max": 40}},
        )
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [in_range.id]


async def test_resolve_members_cross_project_acl_hidden(studies_engine, basics: Basics) -> None:
    """Cross-project members the caller can't see become a hidden count, not an error."""
    auth = _regular_auth()
    svc = _service()
    scenario_service = make_scenario_service(_audit())
    project_service = make_project_service(_audit())
    user_service = make_user_service(auth, _audit(), hasher=make_password_hasher(auth))
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # A second project (owned by sam) with a completed job, and a
        # regular user "bob" who can see only the home project.
        p2 = await project_service.create(session, actor_user_id=basics.user_id, name="P2")
        s2 = await scenario_service.create(
            session, actor_user_id=basics.user_id, project_id=p2.id, name="S2"
        )
        bob = await user_service.create_user(
            session,
            username="bob",
            email="bob@example.com",
            password="bobpassword",
            full_name="Bob",
            role="regular_user",
        )
        session.add(
            ProjectAcl(
                project_id=basics.project_id,
                user_id=bob.id,
                role="viewer",
                granted_by_user_id=basics.user_id,
            )
        )
        home_member = await _complete_job(session, basics, scenario_id=basics.scenario_id)
        await _complete_job(session, basics, scenario_id=s2.id)  # in P2 — bob can't see it

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            project_ids=[basics.project_id, p2.id],
        )
        # Sam (super_user, sees all): both jobs are members, none hidden.
        sam_view = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert len(sam_view.job_ids) == 2
        assert sam_view.acl_hidden == 0
        # Bob: only the home-project member is visible; the P2 one is hidden.
        bob_view = await svc.resolve_members(session, actor_user_id=bob.id, cohort_id=cohort.id)
        assert bob_view.job_ids == [home_member.id]
        assert bob_view.acl_hidden == 1


# --- governance: capability-gated writes, open reads (D6) ---------------


async def test_read_open_to_any_authenticated(studies_engine, basics: Basics) -> None:
    """Any authenticated caller can read a cohort's metadata (D6).

    Reads are no longer gated on project access — the per-project ACL
    only governs which *member results* surface (tested separately).
    """
    auth = _regular_auth()
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        outsider = await _regular_user(session, auth, username="eve")
        fetched = await svc.get(session, actor_user_id=outsider, cohort_id=cohort.id)
        assert fetched.id == cohort.id


async def test_get_missing_raises_not_found(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(NotFoundError):
            await svc.get(session, actor_user_id=basics.user_id, cohort_id=999_999)


async def test_create_is_base_tier_not_curator_only(studies_engine, basics: Basics) -> None:
    """Creating a cohort needs ``cohorts.create``, which base roles hold.

    This test used to assert the opposite — that a plain ``regular_user``
    was refused until granted ``cohort_curator``. Migration **0083**
    deliberately reclassified ``cohorts.create`` as a base-tier grant and
    seeded it onto every base role, alongside its siblings
    ``optimisation.create`` / ``models.create`` / ``vv.create``, so the
    create surfaces stay open by default rather than being locked to
    administrators. `_require_create` accepts *either* ``cohorts.create``
    or the curate capability; only edit and archive stayed curate-only
    (see ``test_update_and_archive_require_curate``).

    Kept rather than deleted, and asserting both directions, because a
    passing "curator can create" on its own would look identical whether
    creation were base-tier or curator-only — which is exactly how the
    old expectation survived the reclassification unnoticed.
    """
    auth = _regular_auth()
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        carl = await _regular_user(session, auth, username="carl")

        # A base role holds cohorts.create, so this is allowed.
        cohort = await svc.create_cohort(
            session,
            actor_user_id=carl,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        assert cohort.id is not None
        assert cohort.created_by_user_id == carl

        # ...and the gate is still a gate: strip the grant and it refuses.
        await _revoke_permission(session, carl, Permission.COHORTS_CREATE)
        with pytest.raises(AccessDeniedError):
            await svc.create_cohort(
                session,
                actor_user_id=carl,
                name="c2",
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
            )


async def test_update_and_archive_require_curate(studies_engine, basics: Basics) -> None:
    """Edit + archive need ``cohorts.curate`` even for an existing cohort."""
    auth = _regular_auth()
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,  # super_user (has curate)
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        dana = await _regular_user(session, auth, username="dana")
        with pytest.raises(AccessDeniedError):
            await svc.update_cohort(session, actor_user_id=dana, cohort_id=cohort.id, name="new")
        with pytest.raises(AccessDeniedError):
            await svc.archive_cohort(session, actor_user_id=dana, cohort_id=cohort.id)


# --- lazy study_result populate (§3.3) ----------------------------------


async def _cohort_rows(session, cohort_id: int) -> list[StudyResult]:
    return list(
        (
            await session.execute(
                select(StudyResult).where(
                    StudyResult.source_kind == "cohort",
                    StudyResult.source_id == cohort_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def test_populate_copies_scalars_and_is_idempotent(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        member = await _complete_job(session, basics, inputs={"h": 20})
        # An existing results row for the job (as a parametric child would
        # have) — populate copies its scalars rather than touching disk.
        session.add(
            StudyResult(
                source_kind="parametric",
                source_id=4242,
                job_id=member.id,
                scalars={"mass": 3.2},
                retained_files=[],
            )
        )
        await session.flush()
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        written = await svc.populate_study_results(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert written == 1
        rows = await _cohort_rows(session, cohort.id)
        assert len(rows) == 1
        assert rows[0].job_id == member.id
        assert rows[0].scalars == {"mass": 3.2}
        # Second call is the cheap diff — nothing new.
        again = await svc.populate_study_results(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert again == 0
        assert len(await _cohort_rows(session, cohort.id)) == 1


async def test_populate_writes_empty_row_when_no_outputs(studies_engine, basics: Basics) -> None:
    """A member with no existing row + no readable outputs lands with empty
    scalars (D3 null-with-banner), never an error."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        member = await _complete_job(session, basics)  # ad-hoc job, no results row, no disk
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        written = await svc.populate_study_results(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert written == 1
        rows = await _cohort_rows(session, cohort.id)
        assert rows[0].job_id == member.id
        assert rows[0].scalars == {}


async def test_populate_normalises_workflow_member_keys(studies_engine, basics: Basics) -> None:
    """An optimisation-workflow source keys scalars ``<member_tool>.<name>``;
    populate collapses them to the cohort tool's bare output, dropping any
    other member's outputs (a different tool's response)."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        member = await _complete_job(session, basics, inputs={"h": 20})
        session.add(
            StudyResult(
                source_kind="optimisation",
                source_id=77,
                job_id=member.id,
                scalars={f"{basics.tool_alias}.heat_dissipation": 14.2, "other.mass": 9.9},
                retained_files=[],
            )
        )
        await session.flush()
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        written = await svc.populate_study_results(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert written == 1
        rows = await _cohort_rows(session, cohort.id)
        assert rows[0].scalars == {"heat_dissipation": 14.2}  # prefix stripped, foreign dropped


async def test_populate_rekeys_legacy_cohort_row(studies_engine, basics: Basics) -> None:
    """A cohort row stored before the fix (mixed keys) is re-keyed in place on
    the next populate, so a surrogate fit sees one column per output."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        member = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        session.add(
            StudyResult(
                source_kind="cohort",
                source_id=cohort.id,
                job_id=member.id,
                scalars={f"{basics.tool_alias}.heat_dissipation": 5.0, "heat_dissipation": None},
                retained_files=[],
            )
        )
        await session.flush()
        written = await svc.populate_study_results(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert written == 0  # the row already existed — re-keyed, not re-created
        rows = await _cohort_rows(session, cohort.id)
        assert rows[0].scalars == {"heat_dissipation": 5.0}


# --- tolerated-version warnings (§3.2) ----------------------------------


async def test_tolerated_version_warnings_flags_divergence(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        anchor = ToolVersion(
            alias=basics.tool_alias,
            version="a",
            descriptor_snapshot={
                "alias": basics.tool_alias,
                "inputs": [{"name": "h", "type": "float", "unit": "mm"}],
            },
            bundle_path="bundle/a",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="a",
            changes={},
        )
        candidate = ToolVersion(
            alias=basics.tool_alias,
            version="b",
            descriptor_snapshot={
                "alias": basics.tool_alias,
                "inputs": [{"name": "h", "type": "float", "unit": "cm"}],  # unit changed
            },
            bundle_path="bundle/b",
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=basics.user_id,
            change_summary="b",
            changes={},
        )
        session.add_all([anchor, candidate])
        await session.flush()

        warnings = await svc.tolerated_version_warnings(
            session,
            tool_alias=basics.tool_alias,
            anchor_version_id=anchor.id,
            candidate_version_id=candidate.id,
        )
        assert any("unit" in w for w in warnings)

        # Identical descriptors → no warning.
        none = await svc.tolerated_version_warnings(
            session,
            tool_alias=basics.tool_alias,
            anchor_version_id=anchor.id,
            candidate_version_id=anchor.id,
        )
        assert none == []


# --- membership model: live vs fixed, exclusions, sources, preview (D4) ---


async def test_fixed_cohort_freezes_picked_runs(studies_engine, basics: Basics) -> None:
    """``auto_grow=False`` freezes ``member_job_ids``; later runs don't join."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        picked = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="fixed",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=False,
            member_job_ids=[picked.id],
        )
        assert cohort.auto_grow is False
        assert cohort.member_job_ids == [picked.id]
        # A new matching run lands — the fixed cohort ignores it.
        await _complete_job(session, basics)
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [picked.id]


async def test_fixed_cohort_rejects_foreign_run(studies_engine, basics: Basics) -> None:
    """A picked run must be a completed run of the tool at a tolerated version."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        running = await _complete_job(session, basics, state="running")  # not complete
        with pytest.raises(CohortConfigError):
            await svc.create_cohort(
                session,
                actor_user_id=basics.user_id,
                name="c",
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
                auto_grow=False,
                member_job_ids=[running.id],
            )


async def test_live_cohort_exclusions(studies_engine, basics: Basics) -> None:
    """``excluded_job_ids`` permanently drops a live match."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        keep = await _complete_job(session, basics)
        drop = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            excluded_job_ids=[drop.id],
        )
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [keep.id]


async def test_resolve_includes_optimisation_source(studies_engine, basics: Basics) -> None:
    """An optimisation-study child run is a member (the cross-source fix)."""
    from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415

    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = OptimisationStudy(
            project_id=basics.project_id, name="opt", created_by_user_id=basics.user_id
        )
        session.add(study)
        await session.flush()
        opt_job = Job(
            optimisation_study_id=study.id,
            name="opt-run",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs={},
            created_by_user_id=basics.user_id,
        )
        session.add(opt_job)
        await session.flush()
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert opt_job.id in resolved.job_ids


async def test_preview_candidates_returns_metadata(studies_engine, basics: Basics) -> None:
    """Preview surfaces matching runs with source + inputs (curate-gated)."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        standalone = await _complete_job(session, basics, inputs={"h": 7})
        preview = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        assert [r.job_id for r in preview.rows] == [standalone.id]
        assert preview.rows[0].source == "scenario"
        assert preview.rows[0].inputs == {"h": 7}
        assert preview.acl_hidden == 0


async def test_preview_requires_curate(studies_engine, basics: Basics) -> None:
    auth = _regular_auth()
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        carl = await _regular_user(session, auth, username="carl")
        with pytest.raises(AccessDeniedError):
            await svc.preview_candidates(
                session,
                actor_user_id=carl,
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
            )


# --- builder metadata: version bands + numeric inputs (§3.2) --------------


def _seed_version(basics: Basics, version: str, *, inputs: list | None = None) -> ToolVersion:
    snapshot: dict = {"alias": basics.tool_alias}
    if inputs is not None:
        snapshot["inputs"] = inputs
    return ToolVersion(
        alias=basics.tool_alias,
        version=version,
        descriptor_snapshot=snapshot,
        bundle_path=f"bundle/{version}",
        created_at=_dt.datetime.now(tz=_dt.UTC),
        created_by_user_id=basics.user_id,
        change_summary=version,
        changes={},
    )


async def test_version_bands_group_by_major_minor(studies_engine, basics: Basics) -> None:
    """Versions group by major.minor; patches share a band; newest band first."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # basics already seeded "1.0". Add a patch, a new minor, and a new major.
        session.add_all(
            [
                _seed_version(basics, "1.0.1"),
                _seed_version(basics, "1.1.0"),
                _seed_version(basics, "2.0.0"),
            ]
        )
        await session.flush()
        meta = await svc.tool_build_meta(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        assert [b.major_minor for b in meta.version_bands] == ["2.0", "1.1", "1.0"]
        band_10 = next(b for b in meta.version_bands if b.major_minor == "1.0")
        assert len(band_10.version_ids) == 2  # 1.0 + 1.0.1 share the band
        assert band_10.latest_version == "1.0.1"


async def test_build_meta_numeric_inputs_from_latest(studies_engine, basics: Basics) -> None:
    """Only int/float inputs of the latest descriptor are offered, label-first."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        session.add(
            _seed_version(
                basics,
                "1.2.0",
                inputs=[
                    {"name": "fin_height", "label": "Fin height (mm)", "type": "float"},
                    {"name": "count", "label": "Count", "type": "integer"},
                    {"name": "material", "label": "Material", "type": "material"},
                    {"name": "mode", "label": "Mode", "type": "select"},
                ],
            )
        )
        await session.flush()
        meta = await svc.tool_build_meta(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )
        by_name = {n.name: n.label for n in meta.numeric_inputs}
        assert set(by_name) == {"fin_height", "count"}  # material / select excluded
        assert by_name["fin_height"] == "Fin height (mm)"  # label, not name


async def test_build_meta_requires_curate(studies_engine, basics: Basics) -> None:
    auth = _regular_auth()
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        carl = await _regular_user(session, auth, username="carl")
        with pytest.raises(AccessDeniedError):
            await svc.tool_build_meta(session, actor_user_id=carl, tool_alias=basics.tool_alias)


async def test_band_cohort_tolerates_future_patch(studies_engine, basics: Basics) -> None:
    """A ``major.minor`` band cohort picks up a patch installed AFTER creation (§3.2)."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        # basics seeds "1.0". Build a live cohort tolerating the whole 1.0 band.
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="band",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[],
            tolerated_version_bands=["1.0"],
        )
        assert cohort.tolerated_version_bands == ["1.0"]
        assert cohort.tolerated_tool_version_ids == [basics.tool_version_id]  # current snapshot
        old_run = await _complete_job(session, basics)  # at 1.0

        # A new patch 1.0.5 is installed later, and a run is produced at it.
        v105 = _seed_version(basics, "1.0.5")
        session.add(v105)
        await session.flush()
        new_run = await _complete_job(session, basics, tool_version_id=v105.id)
        # A run at a different band (2.0.0) must NOT join.
        v200 = _seed_version(basics, "2.0.0")
        session.add(v200)
        await session.flush()
        await _complete_job(session, basics, tool_version_id=v200.id)

        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(resolved.job_ids) == {old_run.id, new_run.id}  # future 1.0.x joined; 2.0 didn't


async def test_update_flips_auto_grow_both_ways(studies_engine, basics: Basics) -> None:
    """Edit can freeze a live cohort to a picked set and thaw it back to live."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        a = await _complete_job(session, basics)
        b = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="c",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        # Live → fixed with just {a}.
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_grow=False,
            member_job_ids=[a.id],
        )
        assert cohort.auto_grow is False
        assert cohort.member_job_ids == [a.id]
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [a.id]
        # Fixed → live: frozen set cleared, both runs are members again.
        await svc.update_cohort(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id, auto_grow=True
        )
        assert cohort.auto_grow is True
        assert cohort.member_job_ids is None
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(resolved.job_ids) == {a.id, b.id}


# --- a fixed cohort must honour its exclusions too -------------------------


async def test_fixed_cohort_honours_excluded_job_ids(studies_engine, basics: Basics) -> None:
    """De-selecting a run from a fixed cohort must keep it out.

    ``excluded_job_ids`` used to be cleared for a fixed cohort on both create and
    update, and resolution never consulted it. That was invisible while the only
    way into a fixed cohort was ``member_job_ids`` — removing a run from the list
    was enough. It stops being invisible as soon as the cohort also auto-includes
    a study (see the test below).
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        keep = await _complete_job(session, basics)
        drop = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="fixed-with-exclusion",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=False,
            member_job_ids=[keep.id, drop.id],
            excluded_job_ids=[drop.id],
        )
        assert cohort.excluded_job_ids == [drop.id]
        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert resolved.job_ids == [keep.id]


async def test_a_dropped_commissioned_run_does_not_come_back(
    studies_engine, basics: Basics
) -> None:
    """The reported bug: summary said one number, plots and numbers said another.

    A fixed cohort that also auto-includes a study cannot drop one of that
    study's runs by editing ``member_job_ids`` — the run is not in that list.
    Only an exclusion can hold it out, and ``_commissioned_rows`` already
    honoured one; it was create/update that threw it away. Every consumer of the
    membership took the larger set, including a surrogate fitted from the cohort.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        picked = await _complete_job(session, basics)
        study_id, commissioned = await _commissioned_study(session, basics, n=2)
        commissioned_keep, commissioned_drop = commissioned

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="fixed-plus-commissioned",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=False,
            member_job_ids=[picked.id],
        )
        cohort.auto_include_study_ids = [study_id]
        await session.flush()

        before = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(before.job_ids) == {picked.id, commissioned_keep.id, commissioned_drop.id}

        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            excluded_job_ids=[commissioned_drop.id],
        )
        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert commissioned_drop.id not in after.job_ids
        assert set(after.job_ids) == {picked.id, commissioned_keep.id}


# --- the preview names the study and who launched it -----------------------


async def test_preview_names_the_study_a_run_came_from(studies_engine, basics: Basics) -> None:
    """ "Parametric study" does not identify *which* one.

    An archive with several studies showed a column of identical source chips,
    so picking the runs of one of them meant cross-referencing job ids by hand.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        standalone = await _complete_job(session, basics)
        study_id, (child,) = await _commissioned_study(session, basics, n=1)
        study_name = await session.scalar(
            select(ParametricStudy.name).where(ParametricStudy.id == study_id)
        )

        preview = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        by_id = {r.job_id: r for r in preview.rows}

    assert by_id[child.id].source == "parametric_study"
    assert by_id[child.id].source_name == study_name
    # A standalone run has no study to name — blank, not a fabricated one.
    assert by_id[standalone.id].source_name is None


async def test_preview_reports_who_launched_each_run(studies_engine, basics: Basics) -> None:
    """The other thing a curator sorts by when deciding what belongs."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        mine = await _complete_job(session, basics)
        preview = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )

    assert {r.job_id: r.created_by_user_id for r in preview.rows}[mine.id] == basics.user_id


async def test_preview_filters_by_who_launched_the_run(
    studies_engine, basics: Basics, outsider_user_id: int
) -> None:
    """The filter the query always supported and the builder never offered."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        mine = await _complete_job(session, basics)
        theirs = await _complete_job(session, basics, created_by_user_id=outsider_user_id)

        everyone = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        just_mine = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            owner_user_ids=[basics.user_id],
        )

    assert {mine.id, theirs.id} <= {r.job_id for r in everyone.rows}
    assert {r.job_id for r in just_mine.rows} == {mine.id}


# --- builder facets + preview-narrowing filters ----------------------------


async def test_build_meta_lists_only_people_who_ran_this_tool(
    studies_engine, basics: Basics, outsider_user_id: int
) -> None:
    """The picker must offer names that can actually match something.

    Populated from the whole user directory it listed people who had never run
    the tool, leaving the curator to hunt for the few names that do anything.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await _complete_job(session, basics)
        meta = await svc.tool_build_meta(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )

    ids = {f.id for f in meta.launchers}
    assert basics.user_id in ids
    assert outsider_user_id not in ids, "a user who never ran this tool was offered"


async def test_build_meta_lists_the_studies_behind_the_runs(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        await _complete_job(session, basics)  # standalone — no study
        study_id, _ = await _commissioned_study(session, basics, n=2)
        meta = await svc.tool_build_meta(
            session, actor_user_id=basics.user_id, tool_alias=basics.tool_alias
        )

    assert [f.id for f in meta.parametric_studies] == [study_id]
    assert meta.parametric_studies[0].name.startswith("commissioned-")
    assert meta.optimisation_studies == []


async def test_preview_narrows_by_source_kind(studies_engine, basics: Basics) -> None:
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        standalone = await _complete_job(session, basics)
        _study_id, (child,) = await _commissioned_study(session, basics, n=1)

        async def preview(**kw):
            return await svc.preview_candidates(
                session,
                actor_user_id=basics.user_id,
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
                **kw,
            )

        everything = await preview()
        only_studies = await preview(source_kinds=["parametric_study"])
        only_standalone = await preview(source_kinds=["scenario"])

    assert {standalone.id, child.id} <= {r.job_id for r in everything.rows}
    assert {r.job_id for r in only_studies.rows} == {child.id}
    assert {r.job_id for r in only_standalone.rows} == {standalone.id}


async def test_preview_narrows_to_a_named_study(studies_engine, basics: Basics) -> None:
    """Two studies of the same tool must be separable by name, not just kind."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        wanted_id, (wanted,) = await _commissioned_study(session, basics, n=1)
        _other_id, (other,) = await _commissioned_study(session, basics, n=1)

        picked = await svc.preview_candidates(
            session,
            actor_user_id=basics.user_id,
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            source_kinds=["parametric_study"],
            parametric_study_ids=[wanted_id],
        )

    ids = {r.job_id for r in picked.rows}
    assert wanted.id in ids
    assert other.id not in ids


# --- membership changes must not leave stale results behind ----------------


async def test_populate_removes_results_for_runs_that_left_the_cohort(
    studies_engine, basics: Basics
) -> None:
    """The reported bug: a cohort of 80 runs whose coverage read 100.

    ``populate_study_results`` only ever topped the table up, so a run swapped
    out of the membership kept its row. The cohort page showed the new count
    while coverage and ``collect_training_data`` read the union of everything
    that had *ever* been a member — and a surrogate fitted from the cohort
    trained on runs the curator had removed.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        keep = await _complete_job(session, basics)
        drop = await _complete_job(session, basics)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="swaps-a-member",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=False,
            member_job_ids=[keep.id, drop.id],
        )
        await svc.populate_study_results(session, actor_user_id=basics.user_id, cohort_id=cohort.id)

        async def rows() -> set[int]:
            return set(
                (
                    await session.execute(
                        select(StudyResult.job_id).where(
                            StudyResult.source_kind == "cohort",
                            StudyResult.source_id == cohort.id,
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert await rows() == {keep.id, drop.id}

        # Swap one member out, exactly as re-picking the runs would.
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            member_job_ids=[keep.id],
        )
        await svc.populate_study_results(session, actor_user_id=basics.user_id, cohort_id=cohort.id)

        assert await rows() == {keep.id}, "a run that left the cohort kept its result row"


# --- source filters are stored, so an edited cohort still shows them -------


async def test_source_filters_persist_and_narrow_membership(studies_engine, basics: Basics) -> None:
    """Migration 0097. They shipped preview-only, which lost them on re-open."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        standalone = await _complete_job(session, basics)
        study_id, (child,) = await _commissioned_study(session, basics, n=1)

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="studies-only",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=True,
            source_kinds=["parametric_study"],
            parametric_study_ids=[study_id],
        )
        assert cohort.source_kinds == ["parametric_study"]
        assert cohort.parametric_study_ids == [study_id]

        resolved = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )

    assert child.id in resolved.job_ids
    assert standalone.id not in resolved.job_ids, "a stored source filter did not narrow"


async def test_an_unknown_source_kind_is_rejected(studies_engine, basics: Basics) -> None:
    """A typo would otherwise match nothing and read as 'no runs qualify'."""
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(CohortConfigError, match="Unknown run source"):
            await svc.create_cohort(
                session,
                actor_user_id=basics.user_id,
                name="bad-kind",
                tool_alias=basics.tool_alias,
                tolerated_tool_version_ids=[basics.tool_version_id],
                auto_grow=True,
                source_kinds=["parametric"],  # not a kind — it is parametric_study
            )


# --- detaching a commissioned study (2026-09-02) ---------------------------


async def _fixed_plus_commissioned(session, basics: Basics, *, n: int = 2):
    """A fixed cohort of one picked run, with an ``n``-run study attached."""
    svc = _service()
    picked = await _complete_job(session, basics)
    study_id, commissioned = await _commissioned_study(session, basics, n=n)
    cohort = await svc.create_cohort(
        session,
        actor_user_id=basics.user_id,
        name=f"detachable-{next(_NAME_SEQ)}",
        tool_alias=basics.tool_alias,
        tolerated_tool_version_ids=[basics.tool_version_id],
        auto_grow=False,
        member_job_ids=[picked.id],
    )
    cohort.auto_include_study_ids = [study_id]
    await session.flush()
    return svc, cohort, picked, study_id, commissioned


async def test_detaching_a_commissioned_study_shrinks_the_cohort(
    studies_engine, basics: Basics
) -> None:
    """The reported symptom, end to end.

    A fixed cohort narrowed to one study still resolved to the commissioned
    runs as well, and there was no way to say otherwise: the field was on the
    update schema and in neither ``_FILTER_FIELDS``, so the API accepted it and
    discarded it at both layers.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc, cohort, picked, _study_id, commissioned = await _fixed_plus_commissioned(
            session, basics
        )

        before = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(before.job_ids) == {picked.id, *(c.id for c in commissioned)}

        updated = await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_include_study_ids=[],
        )
        assert not updated.auto_include_study_ids, "an emptied list must clear to NULL"

        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert after.job_ids == [picked.id]
        assert updated.member_job_ids == [picked.id], "the picked runs are untouched"


async def test_an_unrelated_save_does_not_detach_a_commissioned_study(
    studies_engine, basics: Basics
) -> None:
    """Renaming a cohort must not silently empty its membership.

    The builder only sends the field when it rendered the control, and the
    service leaves a key it was never passed alone. Both halves have to hold,
    or a rename becomes a mass detach.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc, cohort, picked, study_id, commissioned = await _fixed_plus_commissioned(
            session, basics
        )

        updated = await svc.update_cohort(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id, name="renamed"
        )
        assert updated.auto_include_study_ids == [study_id]

        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(after.job_ids) == {picked.id, *(c.id for c in commissioned)}


async def test_a_commissioned_study_can_be_reattached(studies_engine, basics: Basics) -> None:
    """Detaching is a curation decision, not a deletion. It has to be
    reversible without re-running anything."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc, cohort, picked, study_id, commissioned = await _fixed_plus_commissioned(
            session, basics
        )
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_include_study_ids=[],
        )
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_include_study_ids=[study_id],
        )
        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert set(after.job_ids) == {picked.id, *(c.id for c in commissioned)}


async def test_detaching_works_for_a_live_cohort_too(studies_engine, basics: Basics) -> None:
    """Detaching clears the field on a live cohort — but need not shrink it.

    Worth pinning because it is counter-intuitive and a curator will hit it.
    A study-homed run **is** in the auto-grow candidate universe
    (``_candidate_rows`` joins ``ParametricStudy``), so once the commissioning
    link is gone those runs are re-admitted on their own merits: same tool, a
    tolerated version, completed. Detaching removes the *reason* they were
    force-included, not the runs.

    To actually drop them from a live cohort you exclude them, or narrow the
    filters. This asserts the honest behaviour rather than the one the name
    suggests.
    """
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service()
        study_id, commissioned = await _commissioned_study(session, basics, n=2)
        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name=f"live-commissioned-{next(_NAME_SEQ)}",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
            auto_grow=True,
        )
        cohort.auto_include_study_ids = [study_id]
        await session.flush()

        before = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert {c.id for c in commissioned} <= set(before.job_ids)

        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_include_study_ids=[],
        )
        assert not cohort.auto_include_study_ids, "the link itself must be cleared"

        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert {c.id for c in commissioned} <= set(after.job_ids), (
            "a live cohort re-admits them through its own query"
        )

        # ...and an exclusion is what actually removes them.
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            excluded_job_ids=[c.id for c in commissioned],
        )
        excluded = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert not ({c.id for c in commissioned} & set(excluded.job_ids))


async def test_detaching_survives_a_flip_to_fixed(studies_engine, basics: Basics) -> None:
    """Switching mode rewrites the membership; it must not resurrect a study
    the curator detached in the same breath."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc, cohort, picked, _study_id, commissioned = await _fixed_plus_commissioned(
            session, basics
        )
        await svc.update_cohort(
            session,
            actor_user_id=basics.user_id,
            cohort_id=cohort.id,
            auto_grow=False,
            member_job_ids=[picked.id],
            auto_include_study_ids=[],
        )
        after = await svc.resolve_members(
            session, actor_user_id=basics.user_id, cohort_id=cohort.id
        )
        assert after.job_ids == [picked.id]
        assert not ({c.id for c in commissioned} & set(after.job_ids))


async def test_junk_study_ids_are_refused_and_change_nothing(
    studies_engine, basics: Basics
) -> None:
    """A rejected edit must leave the cohort exactly as it was."""
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc, cohort, _picked, study_id, _commissioned = await _fixed_plus_commissioned(
            session, basics
        )
        with pytest.raises(CohortConfigError):
            await svc.update_cohort(
                session,
                actor_user_id=basics.user_id,
                cohort_id=cohort.id,
                auto_include_study_ids=["nonsense"],
            )
        assert cohort.auto_include_study_ids == [study_id]


async def test_populate_backfills_retained_files_for_an_ad_hoc_member(
    studies_engine, basics: Basics
) -> None:
    """A cohort of ad-hoc jobs must carry its file outputs, not just scalars.

    Regression for the seam that broke the NAFEMS ad-hoc archive. A study
    child already has a ``study_result`` row written at completion, so
    ``populate_study_results`` reuses it and never reads disk. An ad-hoc job
    has no such row and takes the disk branch — which looked up the tool with
    ``session.get(Tool, job.tool_alias)``. ``Tool.id`` is the primary key and
    ``alias`` is a separate unique column, so that asked for the row whose
    *id* is the string ``"demo"`` and got ``None``.

    The failure was silent in the worst way: ``_extract_scalars`` needs no
    tool, so the Results matrix was fully populated and only
    ``retained_files`` came back empty — surfacing three steps downstream as
    a field surrogate reporting "0 usable run(s) with a retained snapshot"
    while every ``.vtu`` sat on disk.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)

        # The lookup this regression is about, asserted directly: the alias is
        # not the primary key, so a get() by alias finds nothing.
        assert await session.get(Tool, basics.tool_alias) is None
        tool = (
            await session.execute(select(Tool).where(Tool.alias == basics.tool_alias))
        ).scalar_one_or_none()
        assert tool is not None

        location = (
            await session.execute(
                select(StorageLocation).where(StorageLocation.name == "studies-local")
            )
        ).scalar_one()

        member = await _complete_job(session, basics, inputs={"thickness": 3.0})
        member.storage_location_id = location.id
        member.working_dir_relative = f"job_{member.id:07}"
        await session.flush()

        outputs_dir = Path(location.mount_path) / member.working_dir_relative / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "outputs.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tool_alias": basics.tool_alias,
                    "outputs": [
                        {
                            "name": "peak_temperature",
                            "label": "Peak temperature",
                            "type": "number",
                            "value": 41.5,
                        },
                        {
                            "name": "temperature_field",
                            "label": "Temperature field",
                            "type": "file",
                            "filename": "outputs/temperature_field.vtu",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="ad-hoc",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        assert (
            await svc.populate_study_results(
                session, actor_user_id=basics.user_id, cohort_id=cohort.id
            )
            == 1
        )

        (row,) = await _cohort_rows(session, cohort.id)
        # Scalars kept working throughout — asserting them guards against a
        # "fix" that trades one half of the document for the other.
        assert row.scalars == {"peak_temperature": 41.5}
        assert [(r["name"], r["filename"]) for r in row.retained_files] == [
            ("temperature_field", "outputs/temperature_field.vtu")
        ]


async def test_populate_heals_a_row_whose_retained_files_were_lost(
    studies_engine, basics: Basics
) -> None:
    """An already-written row with an empty file list is repaired from disk.

    The alias/PK fix only helps rows written *after* it: ``populate`` skips a
    member it already has, so every cohort populated before it keeps
    ``retained_files == []`` and its field surrogate keeps failing with the
    files sitting right there. This is the self-heal, so an existing install
    repairs itself on the next populate rather than needing the rows deleted
    by hand.
    """
    svc = _service()
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        location = (
            await session.execute(
                select(StorageLocation).where(StorageLocation.name == "studies-local")
            )
        ).scalar_one()

        member = await _complete_job(session, basics, inputs={"thickness": 4.0})
        member.storage_location_id = location.id
        member.working_dir_relative = f"job_{member.id:07}"
        await session.flush()

        outputs_dir = Path(location.mount_path) / member.working_dir_relative / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / "outputs.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tool_alias": basics.tool_alias,
                    "outputs": [
                        {
                            "name": "peak_temperature",
                            "label": "Peak temperature",
                            "type": "number",
                            "value": 39.0,
                        },
                        {
                            "name": "temperature_field",
                            "label": "Temperature field",
                            "type": "file",
                            "filename": "outputs/temperature_field.vtu",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        cohort = await svc.create_cohort(
            session,
            actor_user_id=basics.user_id,
            name="already-populated",
            tool_alias=basics.tool_alias,
            tolerated_tool_version_ids=[basics.tool_version_id],
        )
        # The damaged row, exactly as the buggy lookup left it: scalars fine,
        # file list empty.
        session.add(
            StudyResult(
                source_kind="cohort",
                source_id=cohort.id,
                job_id=member.id,
                scalars={"peak_temperature": 39.0},
                retained_files=[],
            )
        )
        await session.flush()

        # Nothing new to write — the member is already there — and the repair
        # still happens.
        assert (
            await svc.populate_study_results(
                session, actor_user_id=basics.user_id, cohort_id=cohort.id
            )
            == 0
        )

        (row,) = await _cohort_rows(session, cohort.id)
        assert [r["name"] for r in row.retained_files] == ["temperature_field"]
        assert row.scalars == {"peak_temperature": 39.0}
