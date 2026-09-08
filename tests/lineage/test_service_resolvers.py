"""Slice 2 tests for the remaining per-kind resolvers.

Covers the slice-2 contract from ``designs/data-lineage.md`` §11:

- Scenario / Project resolvers honour the hierarchy ACL (existence-hide
  on no-access; emits the SCENARIO_IN_PROJECT + SUBMITTED_BY edges).
- Material-version resolver is read-open to any authenticated holder
  of ``lineage.view`` (per ADR-0007).
- Approval-request resolver honours design §4.3 visibility predicate
  (requestor / designated approver / ``projects.see_all`` / job-target
  project VIEW); a user with none of these paths sees the node drop.
- Tool-version resolver respects ``tools.see_*_state`` gating —
  ``test``-state tools are hidden from users who don't hold
  ``tools.see_test_state``.

These are unit tests on the resolvers via ``build_graph(depth=1)``;
the wider cross-kind integration story lands when slice 5 wires the
inline tabs and we exercise the full UI flow end-to-end.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    JobService,
    ProjectService,
    ScenarioService,
)
from matofx.lineage import (
    EdgeKind,
    LineageNotFoundError,
    LineageService,
    NodeKind,
)

# ---- scenario + project ---------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_resolver_emits_project_edge_and_honours_acl(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """Alice's scenario resolves for alice; bob (no project ACL) sees it hidden.

    The scenario emits one upstream edge — ``SCENARIO_IN_PROJECT`` —
    and at depth=1 the BFS also fully resolves the project node on
    the far side of that edge (this is the cheapest test that
    exercises the multi-resolver hand-off).
    """
    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project.id, name="S"
        )

    # Alice (project creator) — full graph.
    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.SCENARIO,
            obj_id=scenario.id,
            actor_user_id=alice_id,
            upstream_depth=1,
        )
    by_kind = {n.kind for n in graph.nodes}
    assert NodeKind.SCENARIO in by_kind
    assert NodeKind.PROJECT in by_kind
    centre = next(n for n in graph.nodes if n.kind == NodeKind.SCENARIO)
    assert centre.label == "S"
    assert centre.state == "active"
    relations = {e.relation for e in graph.edges if e.source == (NodeKind.SCENARIO, scenario.id)}
    assert EdgeKind.SCENARIO_IN_PROJECT in relations

    # Admin — projects.see_all sentinel lets them in too.
    async with session_scope(factory) as session:
        admin_graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.SCENARIO,
            obj_id=scenario.id,
            actor_user_id=admin_id,
            upstream_depth=1,
        )
    assert admin_graph.centre == (NodeKind.SCENARIO, scenario.id)

    # Bob — no project ACL, no see-all → existence-hidden.
    async with session_scope(factory) as session:
        with pytest.raises(LineageNotFoundError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.SCENARIO,
                obj_id=scenario.id,
                actor_user_id=bob_id,
                upstream_depth=1,
            )


# ---- material version ----------------------------------------------------


@pytest.mark.asyncio
async def test_material_version_resolver_open_to_all_authenticated(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    audit_writer: AuditWriter,
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """A material version resolves for any authenticated lineage-view holder.

    Materials are deliberately read-open (ADR-0007) so the curator
    can store materials globally without per-project access gates.
    The lineage resolver mirrors that — anyone with ``lineage.view``
    sees the version, regardless of who curated it. The
    ``published_by`` edge to the curator-user fans out alongside.
    """
    from matofx.materials import make_materials_service  # noqa: PLC0415
    from matofx.materials.orm import (  # noqa: PLC0415
        Material,
        MaterialClass,
        MaterialVersion,
    )

    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(lineage_engine)
    mat_service = make_materials_service(audit_writer)  # noqa: F841 — exercised indirectly via session

    async with session_scope(factory) as session:
        mat_class = MaterialClass(name="Metal", description="", state="active")
        session.add(mat_class)
        await session.flush()
        material = Material(
            name="Test Steel",
            class_id=mat_class.id,
            description="",
            steward_user_id=admin_id,
            state="active",
            created_by_user_id=admin_id,
        )
        session.add(material)
        await session.flush()
        version = MaterialVersion(
            material_id=material.id,
            ordinal=1,
            provenance="test",
            publish_state="published",
            created_by_user_id=admin_id,
            created_at=_dt.datetime.now(tz=_dt.UTC),
        )
        session.add(version)
        await session.flush()
        version_id = version.id

    # Alice — no special privilege beyond lineage.view → still sees it.
    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.MATERIAL_VERSION,
            obj_id=version_id,
            actor_user_id=alice_id,
            upstream_depth=1,
        )
    centre = next(n for n in graph.nodes if n.kind == NodeKind.MATERIAL_VERSION)
    assert centre.label == "Material v1"
    assert centre.state == "published"
    # PUBLISHED_BY edge to admin (the curator).
    pub_edges = [e for e in graph.edges if e.relation == EdgeKind.PUBLISHED_BY]
    assert len(pub_edges) == 1
    assert pub_edges[0].target == (NodeKind.USER, admin_id)


# ---- surrogate model version (ADR-0008 §10) ------------------------------


@pytest.mark.asyncio
async def test_surrogate_lineage_job_edge_and_version_resolver(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001 — the 'demo' tool the study + job ride on
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """A surrogate-tool job draws the RAN_SURROGATE_VERSION edge, and the
    version node resolves with its fitter + source-study provenance (§7.4/§10).

    Builds: a parametric study (the fit source) + a surrogate model and
    version fitted from it; a job tagged with that version (the §7.4
    lineage tag). Centred on the job at depth 2, the graph carries the
    job → surrogate-version edge, the version node, and the version's
    ``published_by`` (fitter) + ``derived_from`` (source study) edges.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.models.orm import SurrogateModel, SurrogateModelVersion  # noqa: PLC0415
    from matofx.studies.orm import ParametricStudy  # noqa: PLC0415
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(lineage_engine)

    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project.id, name="S"
        )
        tv_id = (
            await session.execute(select(ToolVersion.id).where(ToolVersion.alias == "demo"))
        ).scalar_one()
        study = ParametricStudy(
            project_id=project.id,
            name="fit-src",
            tool_alias="demo",
            tool_version_id=int(tv_id),
            mode="grid",
            factor_spec={},
            sampling_params={},
            run_cap=100,
            state="complete",
            generated_count=0,
            skipped_invalid_count=0,
            created_by_user_id=alice_id,
        )
        session.add(study)
        await session.flush()
        model = SurrogateModel(
            name="Demo GP",
            tags=[],
            tool_alias="demo",
            scope="personal",
            owner_user_id=alice_id,
            created_by_user_id=alice_id,
        )
        session.add(model)
        await session.flush()
        version = SurrogateModelVersion(
            surrogate_model_id=model.id,
            ordinal=1,
            kind="scalar",
            factors=[{"name": "x", "min": 1.0, "max": 6.0}],
            responses=[{"name": "y", "kind": "scalar"}],
            source_kind="parametric",
            source_id=study.id,
            tool_version_ids_at_fit=[int(tv_id)],
            n_training_points=6,
            bundle_path="bundle/m/v1",
            bundle_sha256="0" * 64,
            quality={},
            publish_state="published",
            fitted_by_user_id=alice_id,
        )
        session.add(version)
        await session.flush()
        version_id = version.id
        study_id = study.id

        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario.id,
            name="pred",
            tool_alias="demo",
        )
        # Tag the job as if it were a surrogate-tool run (the create path
        # tags this from the descriptor pin; here we set it directly so the
        # test doesn't need a synthesised tool).
        tagged = await session.get(Job, job.id)
        assert tagged is not None
        tagged.surrogate_model_version_id = version_id
        await session.flush()
        job_id = job.id

    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.JOB,
            obj_id=job_id,
            actor_user_id=admin_id,
            upstream_depth=2,
        )

    # The job → surrogate-version edge (the §7.4 tag made queryable).
    ran = [e for e in graph.edges if e.relation == EdgeKind.RAN_SURROGATE_VERSION]
    assert len(ran) == 1
    assert ran[0].target == (NodeKind.SURROGATE_MODEL_VERSION, version_id)

    # The version node resolved, with its fitter + source-study edges.
    node = next(n for n in graph.nodes if n.kind == NodeKind.SURROGATE_MODEL_VERSION)
    assert node.label == "Surrogate v1"
    assert node.state == "published"
    pub = [
        e
        for e in graph.edges
        if e.relation == EdgeKind.PUBLISHED_BY
        and e.source == (NodeKind.SURROGATE_MODEL_VERSION, version_id)
    ]
    assert len(pub) == 1
    assert pub[0].target == (NodeKind.USER, alice_id)
    derived = [e for e in graph.edges if e.relation == EdgeKind.DERIVED_FROM]
    assert len(derived) == 1
    assert derived[0].target == (NodeKind.PARAMETRIC_STUDY, study_id)


async def test_optimisation_study_resolver_edges_and_child(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001 — the 'demo' tool the study rides on
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """An OptimisationStudy resolves project / tool / creator edges + its children (§20.1)."""
    from sqlalchemy import select  # noqa: PLC0415

    from matofx.hierarchy.orm import Job  # noqa: PLC0415
    from matofx.lineage import EdgeKind, NodeKind  # noqa: PLC0415
    from matofx.studies.optimisation.orm import OptimisationStudy  # noqa: PLC0415
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P-opt")
        tv_id = int(
            (
                await session.execute(select(ToolVersion.id).where(ToolVersion.alias == "demo"))
            ).scalar_one()
        )
        study = OptimisationStudy(
            project_id=project.id,
            name="opt-lin",
            target_kind="tool",
            tool_alias="demo",
            tool_version_id=tv_id,
            config_json={"target": {"kind": "tool"}},
            state="running",
            created_by_user_id=alice_id,
        )
        session.add(study)
        await session.flush()
        study_id = study.id
        child = Job(
            scenario_id=None,
            optimisation_study_id=study_id,
            name="opt-child",
            tool_alias="demo",
            tool_version_id=tv_id,
            state="complete",
            inputs={},
            created_by_user_id=alice_id,
        )
        session.add(child)
        await session.flush()
        child_id = child.id

    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.OPTIMISATION_STUDY,
            obj_id=study_id,
            actor_user_id=admin_id,
            downstream_depth=1,
        )

    node = next(n for n in graph.nodes if n.kind == NodeKind.OPTIMISATION_STUDY)
    assert node.label == "opt-lin"
    assert node.deep_link == f"/optimisation/{study_id}"
    ref = (NodeKind.OPTIMISATION_STUDY, study_id)
    assert any(e.source == ref and e.target == (NodeKind.PROJECT, project.id) for e in graph.edges)
    assert any(e.source == ref and e.target == (NodeKind.TOOL_VERSION, tv_id) for e in graph.edges)
    assert any(e.source == ref and e.target == (NodeKind.USER, alice_id) for e in graph.edges)
    # The child job's reverse SEEDED_BY_STUDY edge points back at the study.
    seeded = [e for e in graph.edges if e.relation == EdgeKind.SEEDED_BY_STUDY]
    assert any(e.source == (NodeKind.JOB, child_id) and e.target == ref for e in seeded)


# ---- approval request ----------------------------------------------------


@pytest.mark.asyncio
async def test_approval_request_visibility_paths(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """The §4.3 approval-request visibility predicate exercised end-to-end.

    Build:

    - Alice creates project + scenario + job; admin's-job-scenario
      ACL is implicit (super_user sees all).
    - Alice opens a ``pre`` approval gate on the job, naming bob as
      designated approver.

    Then check who can centre lineage on the approval:

    - Alice (requestor) — sees it.
    - Bob (designated approver) — sees it.
    - Admin (``projects.see_all``) — sees it.
    - Charlie (created on the fly; no project ACL, no approver role,
      no sentinel) — does not see it (``LineageNotFoundError``).
    """
    from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(lineage_engine)

    # Set up: alice's job + an approval request on it for bob.
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project.id, name="S"
        )
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
        )
        # ``tool_alias`` + ``tool_version_id`` are denormalised on the
        # approval row so the approver inbox lists without a join (and
        # so the downgrade chain past ``0046`` — which made these
        # nullable for library/material gates — succeeds at test
        # teardown). Set them to match the job for a job-target gate.
        req = ApprovalRequest(
            job_id=job.id,
            kind="pre",
            status="pending",
            requested_at=_dt.datetime.now(tz=_dt.UTC),
            requested_by_user_id=alice_id,
            eligible_approvers=["bob"],
            designated_approver_user_id=bob_id,
            tool_alias=job.tool_alias,
            tool_version_id=job.tool_version_id,
        )
        session.add(req)
        await session.flush()
        req_id = req.id

    # Add a fourth user "charlie" with no project access at all.
    from matofx.config import AuthSettings  # noqa: PLC0415
    from matofx.users import make_password_hasher, make_user_service  # noqa: PLC0415

    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        charlie = await user_service.create_user(
            session,
            username="charlie",
            email="charlie@example.com",
            password="charliepass",
            full_name="Charlie",
        )
        charlie_id = charlie.id
    # Grant lineage.view to charlie via the regular_user role (already
    # widened by the test fixture); charlie's role inherits that grant.

    # Requestor (alice) — visible.
    async with session_scope(factory) as session:
        g = await lineage_service.build_graph(
            session,
            kind=NodeKind.APPROVAL_REQUEST,
            obj_id=req_id,
            actor_user_id=alice_id,
            upstream_depth=1,
        )
    assert g.centre == (NodeKind.APPROVAL_REQUEST, req_id)

    # Designated approver (bob) — visible.
    async with session_scope(factory) as session:
        g = await lineage_service.build_graph(
            session,
            kind=NodeKind.APPROVAL_REQUEST,
            obj_id=req_id,
            actor_user_id=bob_id,
            upstream_depth=1,
        )
    assert g.centre == (NodeKind.APPROVAL_REQUEST, req_id)

    # Admin (projects.see_all sentinel) — visible.
    async with session_scope(factory) as session:
        g = await lineage_service.build_graph(
            session,
            kind=NodeKind.APPROVAL_REQUEST,
            obj_id=req_id,
            actor_user_id=admin_id,
            upstream_depth=1,
        )
    assert g.centre == (NodeKind.APPROVAL_REQUEST, req_id)

    # Charlie — no project access, no approver role, not requestor →
    # the approval is hidden, which (as the centre) collapses to
    # LineageNotFoundError per the existence-hide invariant.
    async with session_scope(factory) as session:
        with pytest.raises(LineageNotFoundError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.APPROVAL_REQUEST,
                obj_id=req_id,
                actor_user_id=charlie_id,
                upstream_depth=1,
            )


# ---- tool version --------------------------------------------------------


@pytest.mark.asyncio
# ---- centre picker (slice 5b) -------------------------------------------


@pytest.mark.asyncio
async def test_list_centres_returns_visible_jobs_for_actor(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """The job-kind picker returns ``id + label`` entries for visible jobs.

    Drives the admin lineage page's centre dropdown (slice 5b fix). The
    centre list intersects with the actor's project ACL, so a user can
    only pick centres they could already see in the owning surface.
    """
    from matofx.lineage import NodeKind  # noqa: PLC0415

    admin_id, alice_id, bob_id = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=alice_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=alice_id, project_id=project.id, name="S"
        )
        job = await job_service.create(
            session,
            actor_user_id=alice_id,
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
        )

    # Alice sees her job in the picker.
    async with session_scope(factory) as session:
        centres = await lineage_service.list_centres(
            session, kind=NodeKind.JOB, actor_user_id=alice_id
        )
    job_ids = {c.obj_id for c in centres}
    assert job.id in job_ids
    alice_entry = next(c for c in centres if c.obj_id == job.id)
    assert "demo" in alice_entry.label  # tool_alias surfaces in the label
    assert "run-1" in alice_entry.label  # the job name does too

    # Bob has no project access → his picker doesn't include alice's job.
    async with session_scope(factory) as session:
        bob_centres = await lineage_service.list_centres(
            session, kind=NodeKind.JOB, actor_user_id=bob_id
        )
    assert job.id not in {c.obj_id for c in bob_centres}

    # Admin (PROJECTS_SEE_ALL) sees everyone's jobs.
    async with session_scope(factory) as session:
        admin_centres = await lineage_service.list_centres(
            session, kind=NodeKind.JOB, actor_user_id=admin_id
        )
    assert job.id in {c.obj_id for c in admin_centres}


@pytest.mark.asyncio
async def test_list_centres_requires_lineage_view(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
) -> None:
    """A user without ``lineage.view`` is refused at the picker call."""
    from matofx.lineage import LineageAccessDeniedError, NodeKind  # noqa: PLC0415

    _, alice_id, _ = seed_users  # regular_user without explicit lineage.view grant
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        with pytest.raises(LineageAccessDeniedError):
            await lineage_service.list_centres(session, kind=NodeKind.JOB, actor_user_id=alice_id)


@pytest.mark.asyncio
async def test_list_centres_reserved_kind_returns_empty(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
) -> None:
    """These kinds return ``[]`` here — JOB_COHORT is still reserved (no resolver);
    OPTIMISATION_STUDY (ADR-0009) + SURROGATE_MODEL_VERSION are implemented but the
    DB has none seeded, so the centre list is empty by data, not by reservation."""
    from matofx.lineage import NodeKind  # noqa: PLC0415

    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        for kind in (
            NodeKind.OPTIMISATION_STUDY,
            NodeKind.JOB_COHORT,
            NodeKind.SURROGATE_MODEL_VERSION,
        ):
            centres = await lineage_service.list_centres(session, kind=kind, actor_user_id=admin_id)
            assert centres == []


async def test_tool_version_state_gating_hides_test_state_from_regular_users(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """A ``test``-state tool's version is hidden from a regular user.

    Admin sees it (holds ``tools.see_test_state`` via the
    administrator default set); alice doesn't and the resolver
    returns ``None`` — which for a centre collapses to
    :class:`LineageNotFoundError`.
    """
    from matofx.tools.orm import Tool, ToolVersion  # noqa: PLC0415

    admin_id, alice_id, _ = seed_users
    factory = make_session_factory(lineage_engine)

    # Seed a "test"-state tool + its version.
    async with session_scope(factory) as session:
        descriptor = {"alias": "in_dev"}
        bundle_path = "bundle/in_dev"
        session.add(
            Tool(
                alias="in_dev",
                name="In Development",
                version="0.1",
                state="test",
                descriptor=descriptor,
                bundle_path=bundle_path,
                installed_by_user_id=admin_id,
            )
        )
        version = ToolVersion(
            alias="in_dev",
            version="0.1",
            descriptor_snapshot=descriptor,
            bundle_path=bundle_path,
            created_at=_dt.datetime.now(tz=_dt.UTC),
            created_by_user_id=admin_id,
            change_summary="Initial seed",
            changes={},
        )
        session.add(version)
        await session.flush()
        version_id = version.id

    # Admin sees the test-state tool version.
    async with session_scope(factory) as session:
        admin_graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.TOOL_VERSION,
            obj_id=version_id,
            actor_user_id=admin_id,
            upstream_depth=1,
        )
    assert admin_graph.centre == (NodeKind.TOOL_VERSION, version_id)

    # Alice doesn't — existence-hidden.
    async with session_scope(factory) as session:
        with pytest.raises(LineageNotFoundError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.TOOL_VERSION,
                obj_id=version_id,
                actor_user_id=alice_id,
                upstream_depth=1,
            )
