"""Slice 1 tests for :class:`LineageService._resolve_job`.

Covers the slice-1 contract from ``designs/data-lineage.md`` §11:

- :meth:`LineageService.build_graph` validates depth + permission
  *before* touching the DB centre.
- ``_resolve_job`` materialises the expected upstream edges for a
  scenario-homed job (submitter / scenario / tool version) and for a
  workflow-child job (workflow run edge added).
- ACL is intersected at resolution time: a job the caller can't see
  via the hierarchy ACL collapses to
  :class:`LineageNotFoundError` (existence-hide).
- The depth=0 (empty) graph case returns the centre alone, no edges.
- :func:`_assert_dag` raises on a forged cycle and passes on a
  well-formed DAG.

Heavier integration coverage (library / material / approval edges via
the per-field provenance tables) lands in slice 2 alongside the
reverse-edge helpers on each domain service.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import (
    JobService,
    ProjectService,
    ScenarioService,
)
from matofx.lineage import (
    EdgeKind,
    LineageAccessDeniedError,
    LineageDepthOutOfRangeError,
    LineageEdge,
    LineageInvariantError,
    LineageNotFoundError,
    LineageService,
    NodeKind,
)
from matofx.lineage.service import _assert_dag

# ---- happy-path resolver -------------------------------------------------


@pytest.mark.asyncio
async def test_centre_job_resolves_with_full_upstream(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001  -- ensures tool row exists
) -> None:
    """The centre Job resolves with every schema-mandated upstream edge.

    Mandatory edges on a scenario-homed standalone job (every FK is
    NOT NULL except workflow_run_id / parametric_study_id):

    - ``submitted_by`` → user
    - ``belongs_to_scenario`` → scenario → project
    - ``ran_tool_version`` → tool_version → installer (PUBLISHED_BY)

    With slice-2 resolvers landed every neighbour now resolves
    fully — the scenario fans out to its project, the tool version
    fans out to its publisher, and the centre's three direct edges
    extend to a five-node graph at depth=1.
    """
    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=admin_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=admin_id, project_id=project.id, name="S"
        )
        job = await job_service.create(
            session,
            actor_user_id=admin_id,
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.JOB,
            obj_id=job.id,
            actor_user_id=admin_id,
            upstream_depth=1,
        )

    # Centre + three direct neighbours = four nodes at depth=1.
    assert graph.centre == (NodeKind.JOB, job.id)
    by_kind = {n.kind for n in graph.nodes}
    assert by_kind == {NodeKind.JOB, NodeKind.USER, NodeKind.SCENARIO, NodeKind.TOOL_VERSION}

    centre_node = next(n for n in graph.nodes if n.kind == NodeKind.JOB)
    assert centre_node.placeholder is False
    assert centre_node.deep_link == f"/jobs/{job.id}"
    # Node label shows the tool's descriptor name ("Demo"), not its
    # internal alias ([[feedback_use_labels_not_names]]); the alias still
    # rides along in the payload for deep-linking.
    assert centre_node.label.startswith("Demo #")
    assert centre_node.payload["tool_alias"] == "demo"

    # Every neighbour now resolves fully — no placeholders post slice 2.
    for neighbour in graph.nodes:
        assert neighbour.placeholder is False, f"unexpected placeholder: {neighbour}"

    # Centre's three outgoing edges + neighbours' own edges deeper than
    # depth=1 get truncation-recorded but no further walking happens.
    centre_edges = [e for e in graph.edges if e.source == (NodeKind.JOB, job.id)]
    assert len(centre_edges) == 3
    relations = {e.relation for e in centre_edges}
    assert relations == {
        EdgeKind.SUBMITTED_BY,
        EdgeKind.BELONGS_TO_SCENARIO,
        EdgeKind.RAN_TOOL_VERSION,
    }
    # No ACL truncation in the happy path.
    assert graph.hidden_by_acl == 0
    assert graph.soft_capped is False


# ---- permission gate -----------------------------------------------------


@pytest.mark.asyncio
async def test_build_graph_requires_lineage_view(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
) -> None:
    """A regular user without ``lineage.view`` is refused before the centre is touched."""
    _, alice_id, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        with pytest.raises(LineageAccessDeniedError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.JOB,
                obj_id=1,  # doesn't even need to exist — permission fires first
                actor_user_id=alice_id,
            )


@pytest.mark.asyncio
async def test_build_graph_passes_for_user_granted_lineage_view(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001  -- fixture side-effect
) -> None:
    """A regular user who *has* been granted ``lineage.view`` can build graphs."""
    _, alice_id, _ = seed_users
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

    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.JOB,
            obj_id=job.id,
            actor_user_id=alice_id,
        )
    assert graph.centre == (NodeKind.JOB, job.id)


# ---- existence-hide on inaccessible centre -------------------------------


@pytest.mark.asyncio
async def test_centre_invisible_to_actor_collapses_to_not_found(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
    grant_lineage_view_to_regular_users: None,  # noqa: ARG001
) -> None:
    """Alice's job + Bob centring on it = :class:`LineageNotFoundError`.

    Both ``lineage.view`` (held) and the per-project ACL (not held)
    must combine: Bob has the permission but no project access, so
    the centre invisibility collapses to existence-hidden.
    """
    _, alice_id, bob_id = seed_users
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

    async with session_scope(factory) as session:
        with pytest.raises(LineageNotFoundError) as excinfo:
            await lineage_service.build_graph(
                session,
                kind=NodeKind.JOB,
                obj_id=job.id,
                actor_user_id=bob_id,
            )
    assert excinfo.value.kind == "job"
    assert excinfo.value.obj_id == job.id


@pytest.mark.asyncio
async def test_centre_nonexistent_id_collapses_to_not_found(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
) -> None:
    """Unknown job id, permission-holding actor → existence-hidden."""
    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        with pytest.raises(LineageNotFoundError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.JOB,
                obj_id=9_999_999,
                actor_user_id=admin_id,
            )


# ---- empty graph (depth=0) ----------------------------------------------


@pytest.mark.asyncio
async def test_depth_zero_returns_centre_only(
    lineage_engine: AsyncEngine,
    project_service: ProjectService,
    scenario_service: ScenarioService,
    job_service: JobService,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    seed_tool: str,  # noqa: ARG001
) -> None:
    """``upstream=0, downstream=0`` → centre node, zero edges."""
    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        project = await project_service.create(session, actor_user_id=admin_id, name="P")
        scenario = await scenario_service.create(
            session, actor_user_id=admin_id, project_id=project.id, name="S"
        )
        job = await job_service.create(
            session,
            actor_user_id=admin_id,
            scenario_id=scenario.id,
            name="run-1",
            tool_alias="demo",
        )

    async with session_scope(factory) as session:
        graph = await lineage_service.build_graph(
            session,
            kind=NodeKind.JOB,
            obj_id=job.id,
            actor_user_id=admin_id,
            upstream_depth=0,
            downstream_depth=0,
        )

    assert graph.centre == (NodeKind.JOB, job.id)
    assert len(graph.nodes) == 1
    assert graph.nodes[0].kind == NodeKind.JOB
    # The centre's seed_edges are present (the resolver always returns
    # them) but the BFS doesn't walk further; the edges list reflects
    # the truncation-at-depth contract.
    assert len(graph.edges) == 3
    # All edges are recorded with hidden_by_depth bumped (the targets
    # weren't resolved at depth=0).
    assert graph.hidden_by_depth == 3


# ---- depth validation ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_depth", [-1, 21, 100])
async def test_depth_out_of_range_raises(
    lineage_engine: AsyncEngine,
    lineage_service: LineageService,
    seed_users: tuple[int, int, int],
    bad_depth: int,
) -> None:
    """Negative or >20 depths are refused before any DB work."""
    admin_id, _, _ = seed_users
    factory = make_session_factory(lineage_engine)
    async with session_scope(factory) as session:
        with pytest.raises(LineageDepthOutOfRangeError):
            await lineage_service.build_graph(
                session,
                kind=NodeKind.JOB,
                obj_id=1,
                actor_user_id=admin_id,
                upstream_depth=bad_depth,
            )


# ---- DAG cycle guard (direct unit test) ----------------------------------


def test_assert_dag_passes_on_empty_edges() -> None:
    """No edges → trivially acyclic."""
    _assert_dag(())


def test_assert_dag_passes_on_well_formed_dag() -> None:
    """A typical job-centred graph (centre → three upstream leaves) is acyclic."""
    job = (NodeKind.JOB, 1)
    user = (NodeKind.USER, 7)
    scenario = (NodeKind.SCENARIO, 3)
    tool = (NodeKind.TOOL_VERSION, 117)
    edges = (
        LineageEdge(source=job, target=user, relation=EdgeKind.SUBMITTED_BY, label="x"),
        LineageEdge(source=job, target=scenario, relation=EdgeKind.BELONGS_TO_SCENARIO, label="x"),
        LineageEdge(source=job, target=tool, relation=EdgeKind.RAN_TOOL_VERSION, label="x"),
    )
    _assert_dag(edges)  # must not raise


def test_assert_dag_raises_on_two_node_cycle() -> None:
    """``A → B → A`` is a back-edge; the guard must fire."""
    a = (NodeKind.JOB, 1)
    b = (NodeKind.JOB, 2)
    edges = (
        LineageEdge(source=a, target=b, relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN, label="x"),
        LineageEdge(source=b, target=a, relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN, label="x"),
    )
    with pytest.raises(LineageInvariantError) as excinfo:
        _assert_dag(edges)
    assert "cycle" in excinfo.value.reason


def test_assert_dag_raises_on_three_node_cycle() -> None:
    """``A → B → C → A`` is a deeper back-edge; the guard must fire."""
    a = (NodeKind.JOB, 1)
    b = (NodeKind.WORKFLOW_RUN, 2)
    c = (NodeKind.PARAMETRIC_STUDY, 3)
    edges = (
        LineageEdge(source=a, target=b, relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN, label="x"),
        LineageEdge(source=b, target=c, relation=EdgeKind.SEEDED_BY_STUDY, label="x"),
        LineageEdge(source=c, target=a, relation=EdgeKind.SEEDED_BY_STUDY, label="x"),
    )
    with pytest.raises(LineageInvariantError):
        _assert_dag(edges)


def test_assert_dag_passes_on_diamond() -> None:
    """``A → B → D``, ``A → C → D`` is a diamond — acyclic, must pass."""
    a = (NodeKind.JOB, 1)
    b = (NodeKind.SCENARIO, 2)
    c = (NodeKind.WORKFLOW_RUN, 3)
    d = (NodeKind.PROJECT, 4)
    edges = (
        LineageEdge(source=a, target=b, relation=EdgeKind.BELONGS_TO_SCENARIO, label="x"),
        LineageEdge(source=a, target=c, relation=EdgeKind.BELONGS_TO_WORKFLOW_RUN, label="x"),
        LineageEdge(source=b, target=d, relation=EdgeKind.SCENARIO_IN_PROJECT, label="x"),
        LineageEdge(source=c, target=d, relation=EdgeKind.SCENARIO_IN_PROJECT, label="x"),
    )
    _assert_dag(edges)  # diamond is a DAG; must not raise
